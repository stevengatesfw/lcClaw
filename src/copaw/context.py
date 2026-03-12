# -*- coding: utf-8 -*-
"""Request-scoped context for CoPaw.

Provides contextvars for passing per-request state (e.g. user's working
directory) to tools and other components that don't receive it directly.
"""
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from .config.utils import get_user_working_dir

# Current request's user_id (from JWT when LAZY_PLATFORM_KEY is set).
# Set by auth middleware, used by runner when AgentRequest.user_id is empty.
_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "current_user_id",
    default=None,
)

# Current request's working directory (per-user).
# Set by runner at start of query_handler, read by file tools and prompt builder.
_current_working_dir: ContextVar[Optional[Path]] = ContextVar(
    "current_working_dir",
    default=None,
)


def set_current_working_dir(path: Optional[Path]) -> None:
    """Set the working directory for the current request context."""
    _current_working_dir.set(path)


def reset_current_working_dir() -> None:
    """Reset the working directory context (call at end of request)."""
    _current_working_dir.set(None)


def set_current_user_id(user_id: Optional[str]) -> None:
    """Set the user_id for the current request context (from JWT)."""
    _current_user_id.set(user_id)


def reset_current_user_id() -> None:
    """Reset the user_id context (call at end of request)."""
    _current_user_id.set(None)


def get_context_user_id() -> Optional[str]:
    """Get the user_id for the current request (from context, set by auth middleware)."""
    return _current_user_id.get()


def get_current_working_dir() -> Path:
    """Get the working directory for the current request.

    Returns the request-scoped value if set, otherwise users/default.
    """
    val = _current_working_dir.get()
    if val is not None:
        return val
    return get_user_working_dir(None)
