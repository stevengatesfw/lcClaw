# -*- coding: utf-8 -*-
"""Fetch LCAgent published apps when agent/process meta has no list (IM channels).

Console traffic goes through LCAgent ``CopawAgentProcessApi``, which injects
``meta.lcagent_published_apps``. Direct CoPaw channel requests skip that proxy;
this module backfills the same list using ``GET /console/api/home/published-apps``
with the request Bearer JWT (from browser or :mod:`channel_service_token`).

*console_api_base* matches ``lcagent_console_api_base`` (host root without suffix),
e.g. ``http://api:8087``. Full URL is ``{base}/console/api/home/published-apps``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _normalize_auth_header(authorization: str) -> str:
    a = (authorization or "").strip()
    if not a:
        return ""
    if a.lower().startswith("bearer "):
        return a
    return f"Bearer {a}"


async def fetch_visible_published_apps_async(
    console_api_base: str,
    authorization: str,
) -> Optional[list[dict[str, Any]]]:
    """Return apps visible to the JWT user, or None on failure."""
    base = (console_api_base or "").strip().rstrip("/")
    auth = _normalize_auth_header(authorization)
    if not base or not auth:
        return None
    url = f"{base}/console/api/home/published-apps"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("published-apps fetch failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "published-apps HTTP %s url=%s snippet=%s",
            resp.status_code,
            url,
            (resp.text or "")[:200],
        )
        return None
    try:
        data = resp.json()
    except Exception:
        logger.warning("published-apps response not JSON", exc_info=True)
        return None
    if not isinstance(data, dict) or data.get("status") != 0:
        logger.warning(
            "published-apps unexpected payload: %s",
            str(data)[:400],
        )
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    apps = result.get("apps")
    if not isinstance(apps, list):
        return None
    out: list[dict[str, Any]] = []
    for item in apps:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "").strip()
        if not aid:
            continue
        name = str(item.get("name") or "").strip() or aid
        link = str(item.get("app_link") or "").strip()
        desc = str(item.get("description") or "").strip()
        row: dict[str, Any] = {
            "id": aid,
            "name": name,
            "app_link": link,
        }
        if desc:
            row["description"] = desc
        out.append(row)
    return out or None
