# -*- coding: utf-8 -*-
"""FastAPI dependencies for per-user storage paths (LAZY_PLATFORM_KEY)."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException

from ..config.utils import (
    copaw_storage_isolation_enabled,
    get_config_path,
    get_config_path_for_user,
    get_jobs_path_for_user,
    get_user_working_dir,
)
from .auth import get_current_user_id_required


def get_storage_config_path(
    uid: str = Depends(get_current_user_id_required),
) -> Path:
    """Root config.json or users/<uid>/config.json."""
    if copaw_storage_isolation_enabled():
        if not uid:
            raise HTTPException(
                status_code=401,
                detail="请先登录 LCAgent 后再使用 CoPaw。",
            )
        return get_config_path_for_user(uid)
    return get_config_path()


def get_storage_jobs_path(
    uid: str = Depends(get_current_user_id_required),
) -> Path:
    """Legacy jobs.json or users/<uid>/jobs.json."""
    if copaw_storage_isolation_enabled():
        if not uid:
            raise HTTPException(
                status_code=401,
                detail="请先登录 LCAgent 后再使用 CoPaw。",
            )
        return get_jobs_path_for_user(uid)
    from ..config.utils import get_jobs_path

    return get_jobs_path()


def get_storage_envs_json_path(
    uid: str = Depends(get_current_user_id_required),
) -> Path:
    """Global SECRET_DIR/envs.json or users/<uid>/envs.json."""
    if copaw_storage_isolation_enabled():
        if not uid:
            raise HTTPException(
                status_code=401,
                detail="请先登录 LCAgent 后再使用 CoPaw。",
            )
        return (get_user_working_dir(uid) / "envs.json").expanduser()
    from ..envs.store import get_envs_json_path

    return get_envs_json_path()
