# -*- coding: utf-8 -*-
"""LCAgent platform callbacks (Flask console API) from CoPaw tools."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...context import get_process_request_meta, get_request_authorization

logger = logging.getLogger(__name__)


def _lcagent_tool_text(text: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
    )


def invoke_lcagent_published_app(  # pylint: disable=too-many-return-statements,too-many-branches
    query: str,
    app_id: str = "",
) -> ToolResponse:
    """调用 LCAgent 上已发布的工作流应用，返回应用主回复文本。

    已发布（status 正常）且当前用户可见的应用均可调用，不依赖单独开启「API / API 调用」开关。
    用户在浏览器可使用 ``{控制台根}/agent/<应用UUID>`` 打开同一应用（与控制台「复制应用链接」一致）；
    meta 中若有 ``lcagent_console_public_base``，可与系统提示中的链接说明一致。

    将 app_id 留空时使用 meta 中的 lcagent_published_app_id（由 LCAgent 代理按可见已发布应用解析）。
    meta 中可能还有 lcagent_published_app_name / lcagent_published_app_description，
    回答用户「应用是做什么的」时请用这些信息。

    Args:
        query: 转发给应用的用户问题或指令。
        app_id: LCAgent 应用 UUID；空字符串时使用 meta.lcagent_published_app_id。

    经 LCAgent 代理且用户在本轮消息中上传附件时，meta ``lcagent_user_attachment_paths`` 会列出
    服务端路径，本工具会随 ``run_app`` 一并提交，工作流可收到与控制台一致的 input_files。

    Returns:
        ``ToolResponse``，正文为应用输出（可能含文件 URL）或错误说明。
        其中的 ``/app/upload/``、``/console/api/files/download`` 等资源在
        **LCAgent 服务器** 上，勿在当前环境用本地文件工具去读取；
        把链接或 Markdown 原样给用户即可。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    if not base:
        return _lcagent_tool_text(
            "错误：缺少 lcagent_console_api_base。"
            "请通过 LCAgent 的 CoPaw 代理（POST /console/api/copaw/agent/process）访问。",
        )
    aid = (app_id or "").strip() or str(
        meta.get("lcagent_published_app_id") or ""
    ).strip()
    if not aid:
        return _lcagent_tool_text(
            "错误：未指定应用 ID，且 meta 中无 lcagent_published_app_id。"
            "请在参数 app_id 中填写目标应用的 UUID；用户可从 LCAgent 控制台「复制应用链接」"
            "（形如 /agent/<UUID>）中取得。",
        )
    auth = get_request_authorization().strip()
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法以当前用户调用 LCAgent。")

    url = f"{base}/console/api/copaw/lcagent/run_app"
    payload: dict[str, Any] = {"app_id": aid, "query": query}
    raw_att = meta.get("lcagent_user_attachment_paths")
    if isinstance(raw_att, list) and raw_att:
        paths = [str(p).strip() for p in raw_att if str(p).strip()]
        if paths:
            payload["file_paths"] = paths

    try:
        with httpx.Client(
            timeout=httpx.Timeout(600.0, connect=30.0)
        ) as client:
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
        return _lcagent_tool_text(f"调用 LCAgent 失败（网络）: {exc}")

    snippet = (resp.text or "")[:800]
    if resp.status_code != 200:
        logger.warning(
            "invoke_lcagent_published_app: HTTP %s url=%s snippet=%s",
            resp.status_code,
            url,
            snippet[:200],
        )
        return _lcagent_tool_text(f"调用失败: HTTP {resp.status_code} {snippet}")

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return _lcagent_tool_text(f"调用失败: 响应非 JSON {snippet}")

    if not isinstance(data, dict):
        return _lcagent_tool_text(str(data))

    st: Any = data.get("status")
    if st not in (0, None, "0"):
        return _lcagent_tool_text(f"调用失败: {data.get('message') or snippet}")

    result = data.get("result")
    if isinstance(result, dict) and "reply" in result:
        return _lcagent_tool_text(str(result.get("reply") or ""))
    if isinstance(result, str):
        return _lcagent_tool_text(result)
    if result is not None:
        return _lcagent_tool_text(json.dumps(result, ensure_ascii=False))
    return _lcagent_tool_text(snippet)
