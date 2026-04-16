# -*- coding: utf-8 -*-
"""Service-account JWT for IM channels (DingTalk, WeChat, …) calling LCAgent APIs.

Two modes:

1. **Per-workspace owner JWT** (``owner_user_id`` from workspace config path):
   lcClaw calls ``POST /console/api/copaw/issue_token`` on LCAgent (auth via
   ``LCAGENT_COPAW_RESOLVE_SECRET``) to obtain a real JWT + Redis session for
   the workspace owner.  Each user's channels act with that user's identity.

2. **Global service account** (``LCAGENT_CHANNEL_SVC_EMAIL`` +
   ``LCAGENT_CHANNEL_SVC_PASSWORD``): lcClaw POSTs to ``/console/api/login``
   and caches the returned Bearer token.  All channels share one identity.

Mode 1 is preferred when ``owner_user_id`` is available and the resolve-secret
env is set.  Mode 2 is the fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import httpx
import jwt as pyjwt

from ..context import (
    get_context_user_id,
    get_request_authorization,
    set_request_authorization,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _lcagent_api_base() -> str:
    for key in ("LCAGENT_INTERNAL_CONSOLE_API_BASE", "LCAGENT_BACKEND_BASE_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw:
            return raw
    return (os.environ.get("LCAGENT_CONSOLE_API_BASE") or "").strip().rstrip("/")


def _jwt_expiry_epoch(token: str) -> float:
    try:
        payload = pyjwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        logger.debug("channel token: could not decode JWT exp", exc_info=True)
    return time.time() + 24 * 60 * 60 * 30


_LOGIN_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# ---------------------------------------------------------------------------
# Mode 1 — per-workspace owner JWT via /copaw/issue_token
# ---------------------------------------------------------------------------
_OWNER_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_OWNER_TOKEN_LOCK = asyncio.Lock()
_OWNER_TOKEN_REFRESH_MARGIN = 300.0  # refresh 5 min before expiry


async def _fetch_owner_token(user_id: str) -> tuple[str, float]:
    """Call LCAgent ``POST /console/api/copaw/issue_token`` to get a real JWT."""
    base = _lcagent_api_base()
    secret = (os.environ.get("LCAGENT_COPAW_RESOLVE_SECRET") or "").strip()
    if not base or not secret:
        raise RuntimeError("LCAGENT_INTERNAL_CONSOLE_API_BASE or LCAGENT_COPAW_RESOLVE_SECRET not set")

    url = f"{base}/console/api/copaw/issue_token"
    async with httpx.AsyncClient(timeout=_LOGIN_TIMEOUT) as client:
        resp = await client.post(
            url,
            json={"user_id": user_id},
            headers={
                "Content-Type": "application/json",
                "X-LCAgent-Copaw-Resolve-Secret": secret,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"issue_token HTTP {resp.status_code}: {(resp.text or '')[:300]}"
        )
    data = resp.json()
    if not isinstance(data, dict) or data.get("status") not in (0, None, "0"):
        raise RuntimeError(data.get("message") or "issue_token failed")
    result = data.get("result") or {}
    token = (result.get("token") or "").strip()
    if not token:
        raise RuntimeError("issue_token: empty token")
    exp_epoch = float(result.get("exp") or 0)
    if exp_epoch <= 0:
        exp_epoch = _jwt_expiry_epoch(token)
    return token, exp_epoch - _OWNER_TOKEN_REFRESH_MARGIN


async def _get_owner_jwt(user_id: str) -> str:
    """Return a cached owner JWT, re-fetching from LCAgent when near expiry."""
    async with _OWNER_TOKEN_LOCK:
        cached = _OWNER_TOKEN_CACHE.get(user_id)
        if cached is not None:
            tok, exp = cached
            if time.time() < exp:
                return tok
        tok, exp = await _fetch_owner_token(user_id)
        _OWNER_TOKEN_CACHE[user_id] = (tok, exp)
        logger.info(
            "Owner JWT fetched for channel (user_id=%s, exp ~%.0f)",
            user_id,
            exp,
        )
        return tok


# ---------------------------------------------------------------------------
# Mode 2 — global service-account login cache
# ---------------------------------------------------------------------------
_CACHE_LOCK = asyncio.Lock()
_CACHE: dict[str, Any] = {"token": None, "exp": 0.0}


async def _login_refresh() -> str:
    base = _lcagent_api_base()
    email = (os.environ.get("LCAGENT_CHANNEL_SVC_EMAIL") or "").strip()
    password = (os.environ.get("LCAGENT_CHANNEL_SVC_PASSWORD") or "").strip()
    if not base or not email or not password:
        raise RuntimeError(
            "channel service login: set LCAGENT_INTERNAL_CONSOLE_API_BASE "
            "(or LCAGENT_BACKEND_BASE_URL), LCAGENT_CHANNEL_SVC_EMAIL, "
            "LCAGENT_CHANNEL_SVC_PASSWORD",
        )
    url = f"{base}/console/api/login"
    body = {"email": email, "password": password}
    async with httpx.AsyncClient(timeout=_LOGIN_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
        )
    text = (resp.text or "")[:2000]
    if resp.status_code != 200:
        logger.warning(
            "channel service login failed: HTTP %s url=%s snippet=%s",
            resp.status_code,
            url,
            text[:400],
        )
        raise RuntimeError(f"LCAgent login HTTP {resp.status_code}")
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("LCAgent login response is not JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("LCAgent login response is not an object")
    if str(data.get("result") or "") != "success":
        raise RuntimeError(data.get("message") or "LCAgent login failed")
    raw_data = data.get("data")
    if isinstance(raw_data, str) and raw_data.strip():
        token = raw_data.strip()
    elif isinstance(raw_data, dict):
        token = str(raw_data.get("access_token") or raw_data.get("token") or "").strip()
    else:
        token = ""
    if not token:
        raise RuntimeError("LCAgent login: missing token in response data")
    exp = _jwt_expiry_epoch(token) - 300.0
    _CACHE["token"] = token
    _CACHE["exp"] = exp
    logger.info("channel service JWT refreshed (exp ~%.0f)", exp)
    return token


async def get_channel_service_jwt() -> str:
    """Return a valid cached JWT, refreshing when near expiry."""
    async with _CACHE_LOCK:
        now = time.time()
        tok = _CACHE.get("token")
        exp = float(_CACHE.get("exp") or 0.0)
        if isinstance(tok, str) and tok and now < exp:
            return tok
        return await _login_refresh()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def ensure_channel_service_authorization(
    channel: str,
    owner_user_id: Optional[str] = None,
) -> None:
    """Inject ``Authorization: Bearer …`` for LCAgent console API calls.

    Skips when JWT user context exists (browser / agent/process) or when
    Authorization is already set.

    Resolution order:
    1. *owner_user_id* + resolve secret → ``/copaw/issue_token`` (per-user).
    2. ``LCAGENT_CHANNEL_SVC_EMAIL`` / ``PASSWORD`` → login endpoint (global).
    3. Neither configured → skip silently (tools will report "missing auth").
    """
    if (get_context_user_id() or "").strip():
        return
    if (get_request_authorization() or "").strip():
        return
    ch = (channel or "").strip().lower()
    if ch in ("", "console"):
        return

    # Mode 1: per-workspace owner JWT
    owner = (owner_user_id or "").strip()
    secret = (os.environ.get("LCAGENT_COPAW_RESOLVE_SECRET") or "").strip()
    if owner and secret:
        try:
            token = await _get_owner_jwt(owner)
            set_request_authorization(f"Bearer {token}")
            return
        except Exception:
            logger.warning(
                "owner JWT fetch failed for %s", owner, exc_info=True,
            )

    # Mode 2: global service account
    if not (os.environ.get("LCAGENT_CHANNEL_SVC_EMAIL") or "").strip():
        return
    if not (os.environ.get("LCAGENT_CHANNEL_SVC_PASSWORD") or "").strip():
        return
    try:
        token = await get_channel_service_jwt()
    except Exception:
        logger.warning("channel service JWT refresh failed", exc_info=True)
        return
    set_request_authorization(f"Bearer {token}")
