# -*- coding: utf-8 -*-
"""Fetch LCAgent platform home online LLM when agent/process meta is absent.

``LCAGENT_INTERNAL_CONSOLE_API_BASE`` is the API service root without path suffix,
e.g. ``http://api:8087``. Full URL is
``{base}/console/api/copaw/resolve_home_llm``.

Requires ``LCAGENT_COPAW_RESOLVE_SECRET`` matching the LCAgent env of the same name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Optional

from ...providers.models import ResolvedModelConfig

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 5.0


def _fetch_resolve_json(url: str, secret: str) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={"X-LCAgent-Copaw-Resolve-Secret": secret},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except Exception:
        logger.debug("LCAgent resolve_home_llm HTTP failed", exc_info=True)
        return None


async def fetch_lcagent_home_llm_resolved_config() -> Optional[ResolvedModelConfig]:
    """Return platform-resolved LLM config, or None if env unset or request fails."""
    base = (
        os.environ.get("LCAGENT_INTERNAL_CONSOLE_API_BASE") or ""
    ).strip().rstrip("/")
    secret = (os.environ.get("LCAGENT_COPAW_RESOLVE_SECRET") or "").strip()
    if not base or not secret:
        logger.warning(
            "fetch_lcagent_home_llm skipped: LCAGENT_INTERNAL_CONSOLE_API_BASE=%s "
            "LCAGENT_COPAW_RESOLVE_SECRET=%s",
            "set" if base else "EMPTY",
            "set" if secret else "EMPTY",
        )
        return None

    url = f"{base}/console/api/copaw/resolve_home_llm"
    data = await asyncio.to_thread(_fetch_resolve_json, url, secret)
    if not isinstance(data, dict):
        return None
    if data.get("status") != 0:
        logger.warning(
            "LCAgent resolve_home_llm failed (set home LLM to online cloud or "
            "configure agent active_model): %s",
            data.get("message"),
        )
        return None
    raw = data.get("result")
    if not isinstance(raw, dict):
        return None
    try:
        return ResolvedModelConfig.model_validate(raw)
    except Exception:
        logger.warning("Invalid LCAgent resolve_home_llm result", exc_info=True)
        return None
