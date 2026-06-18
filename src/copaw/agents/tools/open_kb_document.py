# -*- coding: utf-8 -*-
"""Open parsed knowledge-base documents by line window."""
from __future__ import annotations

import json
import logging

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...context import get_process_request_meta, get_request_authorization

logger = logging.getLogger(__name__)


def _tool_text(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _resolve_kb_id(kb_id: str | None) -> tuple[str | None, str | None]:
    meta = get_process_request_meta()
    selected = [str(value) for value in (meta.get("lcagent_knowledge_base_ids") or [])]
    if not selected:
        return None, "未选择任何知识库，无法打开文档。"
    if kb_id is not None:
        if str(kb_id) not in selected:
            return None, f"知识库 {kb_id} 不在已选列表中。"
        return str(kb_id), None
    if len(selected) == 1:
        return selected[0], None
    return None, "已选择多个知识库，请传入 search_knowledge_base 返回的 kb_id。"


def open_kb_document(
    file_id: str,
    kb_id: str | None = None,
    line: int | None = None,
    offset: int | None = None,
    window_size: int = 1800,
) -> ToolResponse:
    """Open a knowledge-base file's parsed text by line window.

    Use both ``file_id`` and ``kb_id`` returned by ``search_knowledge_base``.
    ``line`` is 1-based and takes precedence over the 0-based ``offset``.
    """
    resolved_kb_id, error = _resolve_kb_id(kb_id)
    if error:
        return _tool_text(error)

    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base:
        return _tool_text("错误：缺少 lcagent_console_api_base。请通过 LCAgent 的 CoPaw 代理访问。")
    if not auth:
        return _tool_text("错误：缺少 Authorization。")

    payload = {
        "kb_id": resolved_kb_id,
        "file_id": str(file_id),
        "line": line,
        "offset": offset,
        "window_size": window_size,
    }
    url = f"{base}/console/api/kb/document/open"
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            response = client.post(
                url,
                json=payload,
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        logger.warning("open_kb_document: request error %s", exc)
        return _tool_text("知识库原文暂不可用（网络错误）")

    if response.status_code != 200:
        try:
            detail = response.json().get("message")
        except (json.JSONDecodeError, ValueError, AttributeError):
            detail = None
        logger.warning("open_kb_document: HTTP %s url=%s", response.status_code, url)
        return _tool_text(detail or f"打开知识库原文失败: HTTP {response.status_code}")

    try:
        result = response.json()
        start_line = int(result["start_line"])
        total_lines = int(result["total_lines"])
        content_lines = str(result.get("content", "")).splitlines()
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return _tool_text("打开知识库原文失败：响应解析错误")

    numbered = "\n".join(
        f"{start_line + index}: {text}" for index, text in enumerate(content_lines)
    )
    end_line = start_line + len(content_lines) - 1 if content_lines else start_line - 1
    return _tool_text(
        f"文件: {result.get('file_name', file_id)} (ID: {file_id})\n"
        f"行: {start_line}-{end_line} / {total_lines}\n{numbered}",
    )
