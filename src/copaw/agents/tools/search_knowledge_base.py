# -*- coding: utf-8 -*-
"""Knowledge base vector search tool for CoPaw.

Calls back to LCAgent Flask ``/kb/search`` endpoint via httpx,
same pattern as ``invoke_lcagent_published_app``.
"""
from __future__ import annotations

import json
import logging

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...context import get_process_request_meta, get_request_authorization

logger = logging.getLogger(__name__)


def _kb_tool_text(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def search_knowledge_base(
    query: str,
    kb_id: str | None = None,
    top_k: int = 5,
) -> ToolResponse:
    """Search selected knowledge bases for document fragments relevant to the query.

    Args:
        query: Natural language question or search terms.
        kb_id: Restrict to a single knowledge base ID. Pass ``None`` to search
            all user-selected KBs.
        top_k: Maximum number of results to return (default 5).

    Returns:
        ``ToolResponse`` with matched document fragments including content,
        source file, similarity score, and KB name.  Returns empty list or
        error text on failure (never raises).
    """
    meta = get_process_request_meta()
    count = int(meta.get("lcagent_knowledge_base_count") or 0)
    if count <= 0:
        return _kb_tool_text("未选择任何知识库，无法搜索。")

    kb_ids = meta.get("lcagent_knowledge_base_ids") or []

    if kb_id is not None:
        if str(kb_id) not in [str(x) for x in kb_ids]:
            logger.warning("search_knowledge_base: kb_id %s not in selected list", kb_id)
            return _kb_tool_text(f"知识库 {kb_id} 不在已选列表中。")
        search_ids = [str(kb_id)]
    else:
        search_ids = [str(x) for x in kb_ids]

    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    if not base:
        return _kb_tool_text(
            "错误：缺少 lcagent_console_api_base。"
            "请通过 LCAgent 的 CoPaw 代理访问。",
        )

    auth = get_request_authorization().strip()
    if not auth:
        return _kb_tool_text("错误：缺少 Authorization。")

    url = f"{base}/console/api/kb/search"
    payload = {"kb_ids": search_ids, "query": query, "top_k": top_k}

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            resp = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("search_knowledge_base: request error %s", exc)
        return _kb_tool_text("知识库暂不可用（网络错误）")

    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        logger.warning(
            "search_knowledge_base: HTTP %s url=%s snippet=%s",
            resp.status_code,
            url,
            snippet,
        )
        return _kb_tool_text(f"知识库搜索失败: HTTP {resp.status_code}")

    try:
        results = resp.json()
    except (json.JSONDecodeError, ValueError):
        return _kb_tool_text("知识库搜索失败：响应解析错误")

    if not isinstance(results, list) or not results:
        return _kb_tool_text("未找到相关内容。")

    lines: list[str] = []
    for i, item in enumerate(results, 1):
        if "error" in item:
            lines.append(f"[{i}] {item.get('kb_name', '?')}: {item['error']}")
            continue
        src = item.get("source", "")
        score = item.get("score", 0)
        content = item.get("content", "")
        kb_name = item.get("kb_name", "?")
        item_kb_id = item.get("kb_id", "")
        file_id = item.get("file_id")
        file_name = item.get("file_name") or src
        document_ref = (
            f" | KB ID: {item_kb_id} | 文件: {file_name} | 文件 ID: {file_id}"
            if file_id
            else f" | 文件: {file_name} | 文件 ID: 不可用"
        )
        lines.append(
            f"[{i}] 知识库: {kb_name}{document_ref} | 得分: {score:.4f}\n{content}",
        )
    return _kb_tool_text("\n\n".join(lines))
