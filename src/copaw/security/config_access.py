# -*- coding: utf-8 -*-
"""Helpers for resolving security config under per-user storage isolation."""
from __future__ import annotations

from pathlib import Path

from ..config import load_config
from ..config.utils import (
    copaw_storage_isolation_enabled,
    get_config_path,
    get_config_path_for_user,
    get_user_working_dir,
)
from ..constant import WORKING_DIR
from ..context import get_context_user_id


def get_security_config_path() -> Path:
    """Return the effective config path for the current user context."""
    if copaw_storage_isolation_enabled():
        return get_config_path_for_user(get_context_user_id())
    return get_config_path()


def load_security_config():
    """Load config from the current user's effective config path."""
    return load_config(get_security_config_path())


def get_security_working_dir() -> Path:
    """Return the effective working directory for the current user context."""
    if copaw_storage_isolation_enabled():
        return get_user_working_dir(get_context_user_id())
    return WORKING_DIR
