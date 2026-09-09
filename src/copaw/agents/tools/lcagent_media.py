# -*- coding: utf-8 -*-
"""Track invoke_lcagent_published_app media for user-visible reply merging."""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Set
from urllib.parse import unquote, urlparse

_LCAGENT_UPLOAD_PATH_RE = re.compile(r"/app/upload/[^\s\)\]\"'<>]+", re.IGNORECASE)
_LCAGENT_TMP_PATH_RE = re.compile(r"/tmp/[^\s\)\]\"'<>]+", re.IGNORECASE)
_STATIC_UPLOAD_URL_RE = re.compile(
    r"https?://[^\s\)\]\"'<>]+/static/upload(/[^\s\)\]\"'<>]+)",
    re.IGNORECASE,
)

_invoke_lcagent_reply_text: ContextVar[str] = ContextVar(
    "invoke_lcagent_reply_text",
    default="",
)
_invoke_lcagent_media_paths: ContextVar[frozenset[str]] = ContextVar(
    "invoke_lcagent_media_paths",
    default=frozenset(),
)


def reset_invoke_lcagent_media_state() -> None:
    """Clear per-request invoke media tracking (call at reply start/end)."""
    _invoke_lcagent_reply_text.set("")
    _invoke_lcagent_media_paths.set(frozenset())


def _normalize_posix_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/")


def _extract_lcagent_paths_from_text(text: str) -> Set[str]:
    paths: Set[str] = set()
    if not text:
        return paths
    for pattern in (_LCAGENT_UPLOAD_PATH_RE, _LCAGENT_TMP_PATH_RE):
        for match in pattern.finditer(text):
            p = _normalize_posix_path(match.group(0)).rstrip(".,);")
            if p:
                paths.add(p)
    for match in _STATIC_UPLOAD_URL_RE.finditer(text):
        rel = match.group(1) or ""
        if rel:
            paths.add(_normalize_posix_path(f"/app/upload{rel}").rstrip(".,);"))
    return paths


def register_invoke_lcagent_reply(reply: str) -> None:
    """Remember the latest successful invoke_lcagent_published_app user reply."""
    text = (reply or "").strip()
    if not text:
        return
    _invoke_lcagent_reply_text.set(text)
    _invoke_lcagent_media_paths.set(frozenset(_extract_lcagent_paths_from_text(text)))


def get_invoke_lcagent_reply_text() -> str:
    return (_invoke_lcagent_reply_text.get() or "").strip()


def get_invoke_lcagent_media_paths() -> Set[str]:
    return set(_invoke_lcagent_media_paths.get() or frozenset())


def is_lcagent_server_path(posix_path: str) -> bool:
    p = _normalize_posix_path(posix_path)
    return p.startswith("/tmp/") or p.startswith("/app/upload/")


def is_path_covered_by_invoke(file_path: str) -> bool:
    """True when *file_path* was already returned by invoke this request."""
    p = _normalize_posix_path(file_path)
    if not p:
        return False
    registered = get_invoke_lcagent_media_paths()
    if p in registered:
        return True
    base = p.rsplit("/", 1)[-1]
    if not base:
        return False
    return any(r.rsplit("/", 1)[-1] == base for r in registered)


def body_already_has_invoke_media(text: str) -> bool:
    if not text:
        return False
    registered = get_invoke_lcagent_media_paths()
    if not registered:
        return False
    for path in registered:
        if path in text:
            return True
        base = path.rsplit("/", 1)[-1]
        if base and base in text:
            return True
    return False


def invoke_lcagent_user_appendix_for_body() -> str:
    """Text to append to the final assistant message when invoke media is missing."""
    reply = get_invoke_lcagent_reply_text()
    if not reply:
        return ""
    return reply


def coerce_lcagent_static_urls_to_path_tokens(text: str) -> str:
    """``https://host/static/upload/sd/x.jpg`` → ``/app/upload/sd/x.jpg`` in markdown."""

    def _repl(match: re.Match[str]) -> str:
        rel = unquote(match.group(1) or "")
        return _normalize_posix_path(f"/app/upload{rel}")

    return _STATIC_UPLOAD_URL_RE.sub(_repl, text or "")


def static_upload_url_to_lcagent_path(url: str) -> str | None:
    """Map a static-upload http(s) URL to ``/app/upload/...`` if possible."""
    t = (url or "").strip()
    if not t:
        return None
    m = _STATIC_UPLOAD_URL_RE.match(t)
    if m:
        return _normalize_posix_path(f"/app/upload{unquote(m.group(1) or '')}")
    try:
        u = urlparse(t)
        if "/static/upload/" in u.path:
            idx = u.path.index("/static/upload/")
            rel = u.path[idx + len("/static/upload") :]
            return _normalize_posix_path(f"/app/upload{rel}")
    except Exception:
        return None
    return None
