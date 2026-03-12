# -*- coding: utf-8 -*-
"""JWT authentication for LCAgent platform integration.

When LAZY_PLATFORM_KEY is set, verifies JWT from Authorization header
and extracts user_id for chats/session isolation.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


def _get_platform_key() -> Optional[str]:
    """Return LAZY_PLATFORM_KEY if set and non-empty."""
    key = os.environ.get("LAZY_PLATFORM_KEY", "").strip()
    return key if key else None


def verify_lcagent_token(token: str) -> Optional[str]:
    """Verify JWT token and extract user_id.

    Uses LAZY_PLATFORM_KEY as secret. Compatible with LCAgent's
    PassportService.issue payload containing user_id.

    Args:
        token: JWT token string, optionally with "Bearer " prefix.

    Returns:
        user_id from payload if valid, None otherwise.
    """
    key = _get_platform_key()
    if not key:
        return None

    if not token or not isinstance(token, str):
        return None

    if token.startswith("Bearer "):
        token = token[7:].strip()

    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
        return payload.get("user_id")
    except Exception:
        return None


def get_current_user_id(request: Request) -> Optional[str]:
    """FastAPI dependency: extract user_id from JWT in Authorization header.

    When LAZY_PLATFORM_KEY is set:
    - Valid token: returns user_id
    - No/invalid token: returns None (caller may raise 401 for protected routes)

    When LAZY_PLATFORM_KEY is not set: always returns None (no auth required).
    """
    key = _get_platform_key()
    if not key:
        return None

    auth = request.headers.get("Authorization")
    if not auth:
        return None

    return verify_lcagent_token(auth)


def get_current_user_id_required(request: Request) -> str:
    """FastAPI dependency: require valid JWT, raise 401 if missing/invalid.

    Use for routes that require user isolation when LAZY_PLATFORM_KEY is set.
    When LAZY_PLATFORM_KEY is not set, returns empty string (no isolation mode).
    """
    key = _get_platform_key()
    if not key:
        return ""

    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(
            status_code=401,
            detail="请先登录 LCAgent 后再使用 CoPaw。",
        )

    user_id = verify_lcagent_token(auth)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="请先登录 LCAgent 后再使用 CoPaw。",
        )
    return user_id


class UserIdContextMiddleware(BaseHTTPMiddleware):
    """Set user_id in request context from JWT for all API requests.

    When LAZY_PLATFORM_KEY is set and request has valid Authorization header,
    extracts user_id and stores in context. Used by runner when AgentRequest
    does not carry user_id, and by other components needing per-request user.
    """

    async def dispatch(self, request, call_next):
        from ..context import set_current_user_id, reset_current_user_id

        try:
            user_id = verify_lcagent_token(request.headers.get("Authorization", ""))
            if user_id:
                set_current_user_id(user_id)
            response = await call_next(request)
            return response
        finally:
            reset_current_user_id()


class AgentProcessUserInjectMiddleware(BaseHTTPMiddleware):
    """Inject user_id from JWT into POST /api/agent/process request body.

    When LAZY_PLATFORM_KEY is set, overrides user_id in the request body
    with the value from the JWT. Requires valid token when isolation enabled.
    """

    async def dispatch(self, request, call_next):
        if (
            request.method != "POST"
            or not request.url.path.endswith("/process")
            or "/api/agent" not in request.url.path
        ):
            return await call_next(request)

        key = _get_platform_key()
        has_auth = bool((request.headers.get("Authorization") or "").strip())
        if not key:
            logger.info(
                "agent/process: LAZY_PLATFORM_KEY not set, skip JWT inject (has_auth=%s)",
                has_auth,
            )
            return await call_next(request)

        try:
            body = await request.body()
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            return await call_next(request)

        user_id = verify_lcagent_token(request.headers.get("Authorization", ""))
        logger.info(
            "agent/process: has_key=True has_auth=%s resolved_user_id=%s body_user_id=%s",
            has_auth,
            user_id or "(none)",
            data.get("user_id", "(missing)"),
        )
        if not user_id:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=401,
                content={"detail": "请先登录 LCAgent 后再使用 CoPaw。"},
            )
        data["user_id"] = user_id

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(data).encode("utf-8"),
            }

        request = Request(request.scope, receive=receive)
        return await call_next(request)


# Log at module load so kubectl logs show whether key is present in uvicorn process
logger.info("CoPaw auth: LAZY_PLATFORM_KEY configured=%s", bool(_get_platform_key()))