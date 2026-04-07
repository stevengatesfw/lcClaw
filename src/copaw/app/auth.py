# -*- coding: utf-8 -*-
"""Authentication: optional CoPaw login + LCAgent JWT for tenant isolation.

When ``LAZY_PLATFORM_KEY`` is set, HS256 JWT from the LCAgent ``Authorization``
header is verified and ``user_id`` is extracted for per-user storage isolation.

CoPaw's own login (``COPAW_AUTH_ENABLED``) uses HMAC-signed tokens in
``auth.json`` and is orthogonal to LCAgent JWT.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional

import jwt
from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..constant import SECRET_DIR

logger = logging.getLogger(__name__)

AUTH_FILE = SECRET_DIR / "auth.json"

# Token validity: 7 days
TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600

# Paths that do NOT require authentication
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/register",
        "/api/version",
        "/api/settings/language",
    },
)

# Prefixes that do NOT require authentication (static assets)
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/assets/",
    "/logo.png",
    "/copaw-symbol.svg",
)


# ---------------------------------------------------------------------------
# Helpers (reuse SECRET_DIR patterns from envs/store.py)
# ---------------------------------------------------------------------------


def _chmod_best_effort(path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _prepare_secret_parent(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)


# ---------------------------------------------------------------------------
# Password hashing (salted SHA-256, no external deps)
# ---------------------------------------------------------------------------


def _hash_password(
    password: str,
    salt: Optional[str] = None,
) -> tuple[str, str]:
    """Hash *password* with *salt*.  Returns ``(hash_hex, salt_hex)``."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return h, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Verify *password* against a stored hash."""
    h, _ = _hash_password(password, salt)
    return hmac.compare_digest(h, stored_hash)


# ---------------------------------------------------------------------------
# Token generation / verification (HMAC-SHA256, no PyJWT needed)
# ---------------------------------------------------------------------------


def _get_jwt_secret() -> str:
    """Return the signing secret, creating one if absent."""
    data = _load_auth_data()
    secret = data.get("jwt_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        data["jwt_secret"] = secret
        _save_auth_data(data)
    return secret


def create_token(username: str) -> str:
    """Create an HMAC-signed token: ``base64(payload).signature``."""
    import base64

    secret = _get_jwt_secret()
    payload = json.dumps(
        {
            "sub": username,
            "exp": int(time.time()) + TOKEN_EXPIRY_SECONDS,
            "iat": int(time.time()),
        },
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """Verify *token*, return username if valid, ``None`` otherwise."""
    import base64

    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        secret = _get_jwt_secret()
        expected_sig = hmac.new(
            secret.encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.debug("Token verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Auth data persistence (auth.json in SECRET_DIR)
# ---------------------------------------------------------------------------


def _load_auth_data() -> dict:
    """Load ``auth.json`` from ``SECRET_DIR``.

    Returns the parsed dict, or a sentinel with ``_auth_load_error``
    set to ``True`` when the file exists but cannot be read/parsed so
    that callers can fail closed instead of silently bypassing auth.
    """
    if AUTH_FILE.is_file():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load auth file %s: %s", AUTH_FILE, exc)
            return {"_auth_load_error": True}
    return {}


def _save_auth_data(data: dict) -> None:
    """Save ``auth.json`` to ``SECRET_DIR`` with restrictive permissions."""
    _prepare_secret_parent(AUTH_FILE)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _chmod_best_effort(AUTH_FILE, 0o600)


def is_auth_enabled() -> bool:
    """Check whether authentication is enabled via environment variable.

    Returns ``True`` when ``COPAW_AUTH_ENABLED`` is set to a truthy
    value (``true``, ``1``, ``yes``).  The presence of a registered
    user is checked separately by the middleware so that the first
    user can still reach the registration page.
    """
    env_flag = os.environ.get("COPAW_AUTH_ENABLED", "").strip().lower()
    return env_flag in ("true", "1", "yes")


def has_registered_users() -> bool:
    """Return ``True`` if a user has been registered."""
    data = _load_auth_data()
    return bool(data.get("user"))


# ---------------------------------------------------------------------------
# Registration (single-user)
# ---------------------------------------------------------------------------


def register_user(username: str, password: str) -> Optional[str]:
    """Register the single user account.

    Returns a token on success, ``None`` if a user already exists.
    """
    data = _load_auth_data()

    # Only one user allowed
    if data.get("user"):
        return None

    pw_hash, salt = _hash_password(password)
    data["user"] = {
        "username": username,
        "password_hash": pw_hash,
        "password_salt": salt,
    }

    # Ensure jwt_secret exists
    if not data.get("jwt_secret"):
        data["jwt_secret"] = secrets.token_hex(32)

    _save_auth_data(data)
    logger.info("User '%s' registered", username)
    return create_token(username)


def auto_register_from_env() -> None:
    """Auto-register admin user from environment variables.

    Called once during application startup.  If ``COPAW_AUTH_ENABLED``
    is truthy and both ``COPAW_AUTH_USERNAME`` and ``COPAW_AUTH_PASSWORD``
    are set, the admin account is created automatically — useful for
    Docker, Kubernetes, server-panel, and other automated deployments
    where interactive web registration is not practical.

    Skips silently when:
    - authentication is not enabled
    - a user has already been registered
    - either env var is missing or empty
    """
    if not is_auth_enabled():
        return
    if has_registered_users():
        return

    username = os.environ.get("COPAW_AUTH_USERNAME", "").strip()
    password = os.environ.get("COPAW_AUTH_PASSWORD", "").strip()
    if not username or not password:
        return

    token = register_user(username, password)
    if token:
        logger.info(
            "Auto-registered user '%s' from environment variables",
            username,
        )


def update_credentials(
    current_password: str,
    new_username: Optional[str] = None,
    new_password: Optional[str] = None,
) -> Optional[str]:
    """Update the registered user's username and/or password.

    Requires the current password for verification.  Returns a new
    token on success (because the username may have changed), or
    ``None`` if verification fails.
    """
    data = _load_auth_data()
    user = data.get("user")
    if not user:
        return None

    stored_hash = user.get("password_hash", "")
    stored_salt = user.get("password_salt", "")
    if not verify_password(current_password, stored_hash, stored_salt):
        return None

    if new_username and new_username.strip():
        user["username"] = new_username.strip()

    if new_password:
        pw_hash, salt = _hash_password(new_password)
        user["password_hash"] = pw_hash
        user["password_salt"] = salt
        # Rotate JWT secret to invalidate all existing sessions
        data["jwt_secret"] = secrets.token_hex(32)

    data["user"] = user
    _save_auth_data(data)
    logger.info("Credentials updated for user '%s'", user["username"])
    return create_token(user["username"])


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def authenticate(username: str, password: str) -> Optional[str]:
    """Authenticate *username* / *password*.  Returns a token if valid."""
    data = _load_auth_data()
    user = data.get("user")
    if not user:
        return None
    if user.get("username") != username:
        return None
    stored_hash = user.get("password_hash", "")
    stored_salt = user.get("password_salt", "")
    if (
        stored_hash
        and stored_salt
        and verify_password(password, stored_hash, stored_salt)
    ):
        return create_token(username)
    return None


# ---------------------------------------------------------------------------
# LCAgent platform JWT (LAZY_PLATFORM_KEY)
# ---------------------------------------------------------------------------


def _get_platform_key() -> Optional[str]:
    """Return LAZY_PLATFORM_KEY if set and non-empty."""
    key = os.environ.get("LAZY_PLATFORM_KEY", "").strip()
    return key if key else None


def verify_lcagent_token(token: str) -> Optional[str]:
    """Verify LCAgent Passport JWT and extract ``user_id``.

    Uses ``LAZY_PLATFORM_KEY`` as secret. Token may include ``Bearer `` prefix.
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
        uid = payload.get("user_id")
        if uid is None:
            return None
        # LCAgent Passport uses numeric account.id; pathlib rejects Path / int
        # → 500 on config APIs.
        return str(uid)
    except Exception:
        return None


def get_current_user_id(request: Request) -> Optional[str]:
    """Dependency: ``user_id`` from JWT when ``LAZY_PLATFORM_KEY`` is set."""
    key = _get_platform_key()
    if not key:
        return None

    auth = request.headers.get("Authorization")
    if not auth:
        return None

    return verify_lcagent_token(auth)


def get_current_user_id_required(request: Request) -> str:
    """Require valid LCAgent JWT when isolation is on; else empty string."""
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
    """Set ``user_id`` from LCAgent JWT for the request lifecycle."""

    async def dispatch(self, request, call_next):
        from ..context import reset_current_user_id, set_current_user_id

        try:
            user_id = verify_lcagent_token(
                request.headers.get("Authorization", ""),
            )
            if user_id:
                set_current_user_id(user_id)
            response = await call_next(request)
            return response
        finally:
            reset_current_user_id()


class AgentProcessUserInjectMiddleware(BaseHTTPMiddleware):
    """Inject ``user_id`` from JWT into POST .../api/agent/.../process body."""

    async def dispatch(self, request, call_next):
        if (
            request.method != "POST"
            or not request.url.path.endswith("/process")
            or "/api/agent" not in request.url.path
        ):
            return await call_next(request)

        from ..context import (
            reset_process_request_meta,
            reset_request_authorization,
            set_process_request_meta,
            set_request_authorization,
        )

        try:
            body = await request.body()
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            return await call_next(request)

        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        set_process_request_meta(meta)
        set_request_authorization(request.headers.get("Authorization") or "")

        key = _get_platform_key()
        has_auth = bool((request.headers.get("Authorization") or "").strip())
        if not key:
            logger.info(
                "agent/process: LAZY_PLATFORM_KEY not set, "
                "skip JWT inject (has_auth=%s)",
                has_auth,
            )

            async def receive_no_key():
                return {
                    "type": "http.request",
                    "body": body,
                }

            request = Request(request.scope, receive=receive_no_key)
            try:
                return await call_next(request)
            finally:
                reset_process_request_meta()
                reset_request_authorization()

        user_id = verify_lcagent_token(
            request.headers.get("Authorization", "")
        )
        logger.info(
            "agent/process: has_key=True has_auth=%s "
            "resolved_user_id=%s body_user_id=%s",
            has_auth,
            user_id or "(none)",
            data.get("user_id", "(missing)"),
        )
        if not user_id:
            from fastapi.responses import JSONResponse

            reset_process_request_meta()
            reset_request_authorization()
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
        try:
            return await call_next(request)
        finally:
            reset_process_request_meta()
            reset_request_authorization()


# ---------------------------------------------------------------------------
# FastAPI middleware (CoPaw local auth)
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that checks Bearer token on protected routes."""

    async def dispatch(
        self,
        request: Request,
        call_next,
    ) -> Response:
        """Check Bearer token on protected routes; skip public paths."""
        if self._should_skip_auth(request):
            return await call_next(request)

        # LCAgent Passport JWT (HS256) shares the same Authorization header shape
        if _get_platform_key():
            auth_header = request.headers.get("Authorization", "")
            lc_uid = verify_lcagent_token(auth_header)
            if lc_uid:
                request.state.user = lc_uid
                return await call_next(request)

        token = self._extract_token(request)
        if not token:
            return Response(
                content=json.dumps({"detail": "Not authenticated"}),
                status_code=401,
                media_type="application/json",
            )

        user = verify_token(token)
        if user is None:
            return Response(
                content=json.dumps(
                    {"detail": "Invalid or expired token"},
                ),
                status_code=401,
                media_type="application/json",
            )

        request.state.user = user
        return await call_next(request)

    @staticmethod
    def _should_skip_auth(request: Request) -> bool:
        """Return ``True`` when the request does not require auth."""
        if not is_auth_enabled() or not has_registered_users():
            return True

        path = request.url.path

        if request.method == "OPTIONS":
            return True

        if path in _PUBLIC_PATHS or any(
            path.startswith(p) for p in _PUBLIC_PREFIXES
        ):
            return True

        # Only protect /api/ routes
        if not path.startswith("/api/"):
            return True

        # Allow localhost requests without auth (CLI runs locally)
        client_host = request.client.host if request.client else ""
        return client_host in ("127.0.0.1", "::1")

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        """Extract Bearer token from header or WebSocket query param."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        if "upgrade" in request.headers.get("connection", "").lower():
            return request.query_params.get("token")

        token = request.query_params.get("token")
        if token:
            return token
        return None


logger.info(
    "CoPaw auth: LAZY_PLATFORM_KEY configured=%s",
    bool(_get_platform_key()),
)
