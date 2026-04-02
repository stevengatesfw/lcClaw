# -*- coding: utf-8 -*-
"""LCAgent platform callbacks (Flask console API) from CoPaw tools."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ...context import get_process_request_meta, get_request_authorization

logger = logging.getLogger(__name__)


def invoke_lcagent_published_app(query: str, app_id: str = "") -> str:
    """调用 LCAgent 上已发布且开启 API / API 调用的工作流应用，返回应用主回复文本。

    若在 lcClaw 顶栏选择了默认应用，可将 app_id 留空以使用 meta 中的 lcagent_published_app_id。

    Args:
        query: 转发给应用的用户问题或指令。
        app_id: LCAgent 应用 ID；空字符串时使用 meta.lcagent_published_app_id。

    Returns:
        应用输出文本（可能包含文件 URL）。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    if not base:
        return (
            "错误：缺少 lcagent_console_api_base。"
            "请通过 LCAgent 的 CoPaw 代理（POST /console/api/copaw/agent/process）访问。"
        )
    aid = (app_id or "").strip() or str(meta.get("lcagent_published_app_id") or "").strip()
    if not aid:
        return (
            "错误：未指定应用 ID，且 meta 中无 lcagent_published_app_id。"
            "请在参数中填写 app_id，或在 lcClaw 顶栏选择已发布应用。"
        )
    auth = get_request_authorization().strip()
    if not auth:
        return "错误：缺少 Authorization，无法以当前用户调用 LCAgent。"

    url = f"{base}/console/api/copaw/lcagent/run_app"
    payload = {"app_id": aid, "query": query}
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            resp = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("invoke_lcagent_published_app: request error %s", exc)
        return f"调用 LCAgent 失败（网络）: {exc}"

    snippet = (resp.text or "")[:800]
    if resp.status_code != 200:
        return f"调用失败: HTTP {resp.status_code} {snippet}"

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return f"调用失败: 响应非 JSON {snippet}"

    if not isinstance(data, dict):
        return str(data)

    st: Any = data.get("status")
    if st not in (0, None, "0"):
        return f"调用失败: {data.get('message') or snippet}"

    result = data.get("result")
    if isinstance(result, dict) and "reply" in result:
        return str(result.get("reply") or "")
    if isinstance(result, str):
        return result
    if result is not None:
        return json.dumps(result, ensure_ascii=False)
    return snippet
