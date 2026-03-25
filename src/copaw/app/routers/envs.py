# -*- coding: utf-8 -*-
"""API endpoints for environment variable management."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...config.utils import copaw_storage_isolation_enabled
from ...envs import load_envs, save_envs, delete_env_var
from ..storage_deps import get_storage_envs_json_path

router = APIRouter(prefix="/envs", tags=["envs"])


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------


class EnvVar(BaseModel):
    """Single environment variable."""

    key: str = Field(..., description="Variable name")
    value: str = Field(..., description="Variable value")


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get(
    "",
    response_model=List[EnvVar],
    summary="List all environment variables",
)
async def list_envs(
    env_path: Path = Depends(get_storage_envs_json_path),
) -> List[EnvVar]:
    """Return all configured env vars."""
    envs = load_envs(env_path)
    return [EnvVar(key=k, value=v) for k, v in sorted(envs.items())]


@router.put(
    "",
    response_model=List[EnvVar],
    summary="Batch save environment variables",
    description="Replace all environment variables with "
    "the provided dict. Keys not present are removed.",
)
async def batch_save_envs(
    body: Dict[str, str],
    env_path: Path = Depends(get_storage_envs_json_path),
) -> List[EnvVar]:
    """Batch save – full replacement of all env vars."""
    # Validate keys
    for key in body:
        if not key.strip():
            raise HTTPException(
                400,
                detail="Key cannot be empty",
            )
    cleaned = {k.strip(): v for k, v in body.items()}
    save_envs(
        cleaned,
        env_path,
        apply_environ=not copaw_storage_isolation_enabled(),
    )
    return [EnvVar(key=k, value=v) for k, v in sorted(cleaned.items())]


@router.delete(
    "/{key}",
    response_model=List[EnvVar],
    summary="Delete an environment variable",
)
async def delete_env(
    key: str,
    env_path: Path = Depends(get_storage_envs_json_path),
) -> List[EnvVar]:
    """Delete a single env var."""
    envs = load_envs(env_path)
    if key not in envs:
        raise HTTPException(
            404,
            detail=f"Env var '{key}' not found",
        )
    envs = delete_env_var(
        key,
        env_path,
        apply_environ=not copaw_storage_isolation_enabled(),
    )
    return [EnvVar(key=k, value=v) for k, v in sorted(envs.items())]
