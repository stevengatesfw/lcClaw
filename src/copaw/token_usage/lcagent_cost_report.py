# -*- coding: utf-8 -*-
"""Report homepage CoPaw LLM usage to LCAgent cost_audits (call_type=lcclaw)."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from ..context import (
    get_context_user_id,
    get_lcagent_billing_snapshot,
    get_process_request_meta,
    get_process_session_id,
)

logger = logging.getLogger(__name__)

_REPORT_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _lcagent_api_base() -> str:
    return (
        os.environ.get("LCAGENT_INTERNAL_CONSOLE_API_BASE") or ""
    ).strip().rstrip("/")


def _report_secret() -> str:
    return (
        os.environ.get("LCAGENT_TOKEN_REPORT_SECRET")
        or os.environ.get("LCAGENT_COPAW_RESOLVE_SECRET")
        or ""
    ).strip()


def _is_lcagent_home_context(meta: dict[str, Any]) -> bool:
    """Only bill platform-home assistant paths (LCAgent proxy injects meta)."""
    if meta.get("lcagent_resolved_llm"):
        return True
    if str(meta.get("lcagent_tenant_id") or "").strip():
        return True
    if str(meta.get("lcagent_console_api_base") or "").strip():
        return True
    return False


def _billing_meta() -> dict[str, Any]:
    snap = get_lcagent_billing_snapshot()
    meta = snap.get("meta")
    if isinstance(meta, dict) and meta:
        return meta
    return get_process_request_meta()


def _billing_user_id() -> str:
    snap = get_lcagent_billing_snapshot()
    uid = snap.get("user_id")
    if isinstance(uid, str) and uid.strip():
        return uid.strip()
    return (get_context_user_id() or "").strip()


def _billing_session_id(session_id: str | None) -> str | None:
    if session_id and str(session_id).strip():
        return str(session_id).strip()
    snap = get_lcagent_billing_snapshot()
    sid = snap.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return (get_process_session_id() or "").strip() or None


def _resolve_model_name(fallback: str | None) -> str | None:
    meta = _billing_meta()
    resolved = meta.get("lcagent_resolved_llm")
    if isinstance(resolved, dict):
        for key in ("model", "model_name", "model_key"):
            val = str(resolved.get(key) or "").strip()
            if val:
                return val
    fb = (fallback or "").strip()
    return fb or None


def _usage_field(usage: Any, key: str) -> Any:
    """Read usage field from object or mapping without raising."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    try:
        return getattr(usage, key, None)
    except (KeyError, AttributeError, TypeError):
        return None


def _cached_tokens_from_usage(usage: Any) -> int:
    for attr in (
        "cache_read_input_tokens",
        "cached_input_tokens",
        "prompt_cache_hit_tokens",
        "input_tokens_details",
    ):
        val = _usage_field(usage, attr)
        if isinstance(val, dict):
            for k in ("cached_tokens", "cache_read", "cached"):
                inner = val.get(k)
                if inner is not None:
                    try:
                        return max(int(inner), 0)
                    except (TypeError, ValueError):
                        pass
        elif val is not None:
            try:
                return max(int(val), 0)
            except (TypeError, ValueError):
                pass
    return 0


async def report_lcagent_cost_audit(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    model_name: str | None = None,
    usage: Any = None,
    session_id: str | None = None,
) -> None:
    """POST token usage to LCAgent ``/console/api/costaudit/lcclaw_token_report``."""
    pt = max(int(prompt_tokens or 0), 0)
    ct = max(int(completion_tokens or 0), 0)
    if pt <= 0 and ct <= 0:
        logger.info("LCAgent cost report skipped: zero tokens")
        return

    base = _lcagent_api_base()
    secret = _report_secret()
    if not base or not secret:
        logger.info(
            "LCAgent cost report skipped: missing base=%s secret=%s",
            bool(base),
            bool(secret),
        )
        return

    user_id = _billing_user_id()
    if not user_id:
        logger.info("LCAgent cost report skipped: missing user_id")
        return

    meta = _billing_meta()
    if not _is_lcagent_home_context(meta):
        logger.info(
            "LCAgent cost report skipped: not home context meta_keys=%s",
            sorted(meta.keys())[:12],
        )
        return

    tenant_id = str(meta.get("lcagent_tenant_id") or "").strip()
    sid = _billing_session_id(session_id)
    mn = _resolve_model_name(model_name)
    cached = _cached_tokens_from_usage(usage) if usage is not None else 0

    payload = {
        "user_id": user_id,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "prompt_cached_tokens": cached,
        "tenant_id": tenant_id or None,
        "lcagent_tenant_id": tenant_id or None,
        "session_id": sid,
        "model_name": mn,
    }
    url = f"{base}/console/api/costaudit/lcclaw_token_report"
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_REPORT_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "LCAgent cost report failed: status=%s body=%s user=%s",
                resp.status_code,
                (resp.text or "")[:300],
                user_id,
            )
        else:
            logger.info(
                "LCAgent cost report ok: user=%s tokens=%s model=%s",
                user_id,
                pt + ct,
                mn,
            )
    except httpx.RequestError as exc:
        logger.warning(
            "LCAgent cost report request error: %s user=%s",
            exc,
            user_id,
        )
    except Exception as exc:
        logger.warning(
            "LCAgent cost report unexpected error: %s user=%s",
            exc,
            user_id,
            exc_info=True,
        )
