# -*- coding: utf-8 -*-
"""Report CoPaw token usage to LCAgent billing callback after each agent run.

LCAgent's normal app chat path goes through LazyLLM engine which has
built-in ``NodeMetaHook`` that automatically reports token usage to
``POST /console/api/app/report`` → ``handle_engine_report_payload`` →
``CostService.add``.

The homepage assistant (CoPaw) bypasses LazyLLM entirely.  The token
report callback ``POST /console/api/costaudit/lcclaw_token_report`` was
already implemented in LCAgent but never called from CoPaw.  This module
fills the gap with a fire-and-forget HTTP callback.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_LCAGENT_TOKEN_REPORT_TIMEOUT = 30.0


async def report_tokens_after_run(
    console_api_base: str,
    user_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    session_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    model_name: Optional[str] = None,
    wallet_source: Optional[str] = None,
) -> None:
    """Report token usage delta to LCAgent billing.

    Fire-and-forget — never blocks the SSE stream.  Errors are logged and
    swallowed because a failed billing report must not break the user
    experience.
    """
    if not console_api_base or not user_id:
        return
    total = prompt_tokens + completion_tokens
    if total <= 0:
        return

    secret = (os.environ.get("LCAGENT_TOKEN_REPORT_SECRET") or "").strip()
    if not secret:
        logger.warning(
            "LCAGENT_TOKEN_REPORT_SECRET not set, skipping token report",
        )
        return

    base = console_api_base.rstrip("/")
    url = f"{base}/console/api/costaudit/lcclaw_token_report"

    payload: dict = {
        "user_id": user_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if session_id:
        payload["session_id"] = session_id
    if tenant_id:
        payload["lcagent_tenant_id"] = tenant_id
    if model_name:
        payload["model_name"] = model_name
    if wallet_source:
        payload["wallet_source"] = wallet_source

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_LCAGENT_TOKEN_REPORT_TIMEOUT),
        ) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Token report failed: HTTP %s, body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                logger.info(
                    "Token report ok: user=%s pt=%d ct=%d model=%s",
                    user_id,
                    prompt_tokens,
                    completion_tokens,
                    model_name or "-",
                )
    except Exception:
        logger.warning("Token report error", exc_info=True)
