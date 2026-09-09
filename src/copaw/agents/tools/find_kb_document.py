# -*- coding: utf-8 -*-
"""Find text inside parsed knowledge-base documents."""
from __future__ import annotations

import json
import logging

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...context import get_process_request_meta, get_request_authorization
from .open_kb_document import _resolve_kb_id

logger = logging.getLogger(__name__)


def _tool_text(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def find_kb_document(
    file_id: str,
    patterns: list[str],
    kb_id: str | None = None,
    use_regex: bool = False,
    case_sensitive: bool = False,
    max_windows: int = 5,
    window_size: int = 80,
) -> ToolResponse:
    """Locate keyword or regex occurrences inside a knowledge-base file.

    Use both ``file_id`` and ``kb_id`` returned by ``search_knowledge_base``.
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
        "patterns": patterns,
        "use_regex": use_regex,
        "case_sensitive": case_sensitive,
        "max_windows": max_windows,
        "window_size": window_size,
    }
    url = f"{base}/console/api/kb/document/find"
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            response = client.post(
                url,
                json=payload,
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        logger.warning("find_kb_document: request error %s", exc)
        return _tool_text("知识库原文暂不可用（网络错误）")

    if response.status_code != 200:
        try:
            detail = response.json().get("message")
        except (json.JSONDecodeError, ValueError, AttributeError):
            detail = None
        logger.warning("find_kb_document: HTTP %s url=%s", response.status_code, url)
        return _tool_text(detail or f"知识库文档定位失败: HTTP {response.status_code}")

    try:
        result = response.json()
        matches = result.get("matches") or []
    except (json.JSONDecodeError, ValueError, AttributeError):
        return _tool_text("知识库文档定位失败：响应解析错误")

    if not matches:
        return _tool_text(f"文件 {result.get('file_name', file_id)} 中未定位到匹配内容。")

    sections = []
    for match in matches:
        start_line = int(match.get("line", 1))
        snippet_lines = str(match.get("snippet", "")).splitlines()
        numbered = "\n".join(
            f"{start_line + index}: {text}" for index, text in enumerate(snippet_lines)
        )
        sections.append(f"命中行 {start_line}:\n{numbered}")
    return _tool_text(
        f"文件: {result.get('file_name', file_id)} (ID: {file_id})\n\n"
        + "\n\n".join(sections),
    )
