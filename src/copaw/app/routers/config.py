# -*- coding: utf-8 -*-

from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam, Request

from ...config import (
    load_config,
    save_config,
    ChannelConfig,
    ChannelConfigUnion,
    get_available_channels,
)
from ...config.utils import (
    copaw_storage_isolation_enabled,
    get_heartbeat_config_from_path,
)
from ..channels.registry import BUILTIN_CHANNEL_KEYS
from ...config.config import HeartbeatConfig

from ..storage_deps import get_storage_config_path
from ..auth import get_current_user_id_required
from .schemas_config import HeartbeatBody

router = APIRouter(prefix="/config", tags=["config"])


@router.get(
    "/channels",
    summary="List all channels",
    description="Retrieve configuration for all available channels",
)
async def list_channels(
    config_path: Path = Depends(get_storage_config_path),
) -> dict:
    """List all channel configs (filtered by available channels)."""
    config = load_config(config_path)
    available = get_available_channels()

    # Get all channel configs from model_dump and __pydantic_extra__
    all_configs = config.channels.model_dump()
    extra = getattr(config.channels, "__pydantic_extra__", None) or {}
    all_configs.update(extra)

    # Return all available channels (use default config if not saved)
    result = {}
    for key in available:
        if key in all_configs:
            channel_data = (
                dict(all_configs[key])
                if isinstance(all_configs[key], dict)
                else all_configs[key]
            )
        else:
            # Channel registered but no config saved yet, use empty default
            channel_data = {"enabled": False, "bot_prefix": ""}
        if isinstance(channel_data, dict):
            channel_data["isBuiltin"] = key in BUILTIN_CHANNEL_KEYS
        result[key] = channel_data

    return result


@router.get(
    "/channels/types",
    summary="List channel types",
    description="Return all available channel type identifiers",
)
async def list_channel_types() -> List[str]:
    """Return available channel type identifiers (env-filtered)."""
    return list(get_available_channels())


@router.put(
    "/channels",
    response_model=ChannelConfig,
    summary="Update all channels",
    description="Update configuration for all channels at once",
)
async def put_channels(
    channels_config: ChannelConfig = Body(
        ...,
        description="Complete channel configuration",
    ),
    config_path: Path = Depends(get_storage_config_path),
) -> ChannelConfig:
    """Update all channel configs."""
    config = load_config(config_path)
    config.channels = channels_config
    save_config(config, config_path)
    return channels_config


@router.get(
    "/channels/{channel_name}",
    response_model=ChannelConfigUnion,
    summary="Get channel config",
    description="Retrieve configuration for a specific channel by name",
)
async def get_channel(
    channel_name: str = PathParam(
        ...,
        description="Name of the channel to retrieve",
        min_length=1,
    ),
    config_path: Path = Depends(get_storage_config_path),
) -> ChannelConfigUnion:
    """Get a specific channel config by name."""
    available = get_available_channels()
    if channel_name not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Channel '{channel_name}' not found",
        )
    config = load_config(config_path)
    single_channel_config = getattr(config.channels, channel_name, None)
    if single_channel_config is None:
        extra = getattr(config.channels, "__pydantic_extra__", None) or {}
        single_channel_config = extra.get(channel_name)
    if single_channel_config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Channel '{channel_name}' not found",
        )
    return single_channel_config


@router.put(
    "/channels/{channel_name}",
    response_model=ChannelConfigUnion,
    summary="Update channel config",
    description="Update configuration for a specific channel by name",
)
async def put_channel(
    channel_name: str = PathParam(
        ...,
        description="Name of the channel to update",
        min_length=1,
    ),
    single_channel_config: ChannelConfigUnion = Body(
        ...,
        description="Updated channel configuration",
    ),
    config_path: Path = Depends(get_storage_config_path),
) -> ChannelConfigUnion:
    """Update a specific channel config by name."""
    available = get_available_channels()
    if channel_name not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Channel '{channel_name}' not found",
        )
    config = load_config(config_path)

    # Allow setting extra (plugin) channel config
    setattr(config.channels, channel_name, single_channel_config)
    save_config(config, config_path)
    return single_channel_config


@router.get(
    "/heartbeat",
    summary="Get heartbeat config",
    description="Return current heartbeat config (interval, target, etc.)",
)
async def get_heartbeat(
    config_path: Path = Depends(get_storage_config_path),
) -> Any:
    """Return effective heartbeat config (from file or default)."""
    hb = get_heartbeat_config_from_path(config_path)
    return hb.model_dump(mode="json", by_alias=True)


@router.put(
    "/heartbeat",
    summary="Update heartbeat config",
    description="Update heartbeat and hot-reload the scheduler",
)
async def put_heartbeat(
    request: Request,
    body: HeartbeatBody = Body(..., description="Heartbeat configuration"),
    config_path: Path = Depends(get_storage_config_path),
    uid: str = Depends(get_current_user_id_required),
) -> Any:
    """Update heartbeat config and reschedule the heartbeat job."""
    config = load_config(config_path)
    hb = HeartbeatConfig(
        enabled=body.enabled,
        every=body.every,
        target=body.target,
        active_hours=body.active_hours,
    )
    config.agents.defaults.heartbeat = hb
    save_config(config, config_path)

    cron_manager = getattr(request.app.state, "cron_manager", None)
    if cron_manager is not None:
        if copaw_storage_isolation_enabled() and uid:
            await cron_manager.reschedule_user_heartbeat(uid)
        else:
            await cron_manager.reschedule_heartbeat()

    return hb.model_dump(mode="json", by_alias=True)
