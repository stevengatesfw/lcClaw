# -*- coding: utf-8 -*-
"""Request-scoped context for CoPaw.

Provides contextvars for passing per-request state (e.g. user's working
directory) to tools and other components that don't receive it directly.
"""
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional

from .config.utils import (
    copaw_storage_isolation_enabled,
    get_config_path,
    get_config_path_for_user,
    get_user_working_dir,
)

# Current request's user_id (from JWT when LAZY_PLATFORM_KEY is set).
# Set by auth middleware; used when AgentRequest.user_id is empty.
_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "current_user_id",
    default=None,
)

# Current request's working directory (per-user).
# Set by runner in query_handler; read by file tools and prompt builder.
_current_working_dir: ContextVar[Optional[Path]] = ContextVar(
    "current_working_dir",
    default=None,
)

# POST /api/agent/process JSON ``meta`` (set by auth middleware for runner).
_process_request_meta: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "process_request_meta",
    default=None,
)

# Authorization header from upstream agent/process POST (LCAgent callbacks).
_request_authorization: ContextVar[Optional[str]] = ContextVar(
    "request_authorization",
    default=None,
)

# agent/process ``session_id`` (for cost audit billing).
_process_session_id: ContextVar[Optional[str]] = ContextVar(
    "process_session_id",
    default=None,
)

# Billing snapshot survives middleware teardown during SSE streaming.
_lcagent_billing_snapshot: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "lcagent_billing_snapshot",
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
    """user_id for this request (context; set by auth middleware)."""
    return _current_user_id.get()


def set_process_request_meta(meta: Optional[Dict[str, Any]]) -> None:
    """Store ``meta`` from agent/process JSON for the current request."""
    _process_request_meta.set(meta)


def get_process_request_meta() -> Dict[str, Any]:
    """Return agent/process ``meta`` dict (empty if unset)."""
    m = _process_request_meta.get()
    return m if isinstance(m, dict) else {}


def reset_process_request_meta() -> None:
    """Clear process meta context."""
    _process_request_meta.set(None)


def set_request_authorization(value: Optional[str]) -> None:
    """Store Authorization header for the current agent/process request."""
    _request_authorization.set(value)


def get_request_authorization() -> str:
    """Return Authorization header value (empty if unset)."""
    v = _request_authorization.get()
    return v if isinstance(v, str) else ""


def reset_request_authorization() -> None:
    """Clear Authorization context."""
    _request_authorization.set(None)


def set_process_session_id(session_id: Optional[str]) -> None:
    """Store agent/process session_id for the current request."""
    sid = (session_id or "").strip() or None
    _process_session_id.set(sid)


def get_process_session_id() -> Optional[str]:
    """Return agent/process session_id (None if unset)."""
    return _process_session_id.get()


def reset_process_session_id() -> None:
    """Clear process session_id context."""
    _process_session_id.set(None)


def set_lcagent_billing_snapshot(
    *,
    user_id: Optional[str],
    meta: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> None:
    """Capture billing fields for the request (not cleared by middleware)."""
    uid = (user_id or "").strip() or None
    sid = (session_id or "").strip() or None
    meta_copy = dict(meta) if isinstance(meta, dict) else {}
    _lcagent_billing_snapshot.set(
        {
            "user_id": uid,
            "meta": meta_copy,
            "session_id": sid,
        },
    )


def get_lcagent_billing_snapshot() -> Dict[str, Any]:
    """Return billing snapshot dict (empty if unset)."""
    snap = _lcagent_billing_snapshot.get()
    return snap if isinstance(snap, dict) else {}


def reset_lcagent_billing_snapshot() -> None:
    """Clear billing snapshot (call when agent/process stream completes)."""
    _lcagent_billing_snapshot.set(None)


def get_current_working_dir() -> Path:
    """Get the working directory for the current request.

    Returns the request-scoped value if set, otherwise users/default.
    """
    val = _current_working_dir.get()
    if val is not None:
        return val
    return get_user_working_dir(None)


def get_effective_config_path() -> Path:
    """Config.json path for code paths without a FastAPI Request.

    Uses JWT context user_id when storage isolation is on; otherwise global
    ``get_config_path()``. If isolation is on but context has no user_id yet,
    falls back to global path (legacy / cron); prefer passing explicit paths.
    """
    if copaw_storage_isolation_enabled():
        uid = get_context_user_id()
        if uid:
            return get_config_path_for_user(uid)
    return get_config_path()
