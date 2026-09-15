# -*- coding: utf-8 -*-
"""Fast, model-planned and server-controlled retrieval for home chat."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .model_factory import create_model_and_formatter
from ..context import (
    get_process_request_meta,
    get_request_authorization,
)
from ..providers.models import ResolvedModelConfig

logger = logging.getLogger(__name__)

_MAX_RECENT_MESSAGES = 6
_MAX_RECENT_CONTENT = 2000


def _response_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else ""


async def _complete_text(model: Any, messages: list[dict[str, str]]) -> str:
    response = await model(messages=messages, tool_choice="none")
    if not hasattr(response, "__aiter__"):
        return _response_text(response)
    accumulated = ""
    async for chunk in response:
        current = _response_text(chunk)
        if current:
            if current.startswith(accumulated):
                accumulated = current
            elif not accumulated.endswith(current):
                accumulated += current
    return accumulated


def _parse_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    return parsed if isinstance(parsed, dict) else {}


def _fallback_plan(query: str) -> dict[str, Any]:
    return {
        "intent": "knowledge_qa",
        "should_retrieve": True,
        "standalone_query": query,
        "expanded_queries": [],
        "mentioned_kb_names": [],
        "requires_multiple_kbs": False,
    }


async def plan_knowledge_query(
    *,
    query: str,
    recent_conversation: list[dict[str, str]],
    kb_names: list[str],
    llm_cfg: ResolvedModelConfig,
) -> dict[str, Any]:
    """Rewrite follow-ups and expand retrieval terms with thinking disabled."""
    safe_recent = [
        {
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or "")[:_MAX_RECENT_CONTENT],
        }
        for item in recent_conversation[-_MAX_RECENT_MESSAGES:]
        if isinstance(item, dict)
        and item.get("role") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    ]
    planner_cfg = llm_cfg.model_copy(update={"enable_thinking": False})
    model, _ = create_model_and_formatter(llm_cfg=planner_cfg)
    system = """你是知识检索查询规划器。只输出一个 JSON 对象，不解释。
任务：结合最近对话，把当前追问改写为语义完整的独立问题，并给出最多两个仅供检索的扩写查询。
原始问题不会被替换，最终回答仍以原始问题为准。
JSON 字段：
intent: knowledge_qa 或 general_chat；
should_retrieve: 是否需要查已选知识库；
standalone_query: 独立问题；
expanded_queries: 0 到 2 个不同措辞或关键词组合；
mentioned_kb_names: 问题明确点名的知识库名称；
requires_multiple_kbs: 是否明确要求跨库比较或汇总。
不要回答问题，不要虚构知识库名称。"""
    user = json.dumps(
        {
            "selected_knowledge_bases": kb_names,
            "recent_conversation": safe_recent,
            "current_question": query,
        },
        ensure_ascii=False,
    )
    try:
        raw = await asyncio.wait_for(
            _complete_text(
                model,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            ),
            timeout=20.0,
        )
        parsed = _parse_json_object(raw)
    except Exception:  # noqa: BLE001 - retrieval uses a safe deterministic fallback
        logger.warning("Knowledge query planning failed", exc_info=True)
        return _fallback_plan(query)

    fallback = _fallback_plan(query)
    standalone = str(parsed.get("standalone_query") or query).strip()[:4000]
    expanded = [
        str(item).strip()[:1000]
        for item in (parsed.get("expanded_queries") or [])[:2]
        if str(item).strip()
    ]
    mentioned = [
        str(item).strip()[:200]
        for item in (parsed.get("mentioned_kb_names") or [])[:5]
        if str(item).strip() and str(item).strip() in kb_names
    ]
    intent = str(parsed.get("intent") or fallback["intent"])
    return {
        "intent": intent if intent in {"knowledge_qa", "general_chat"} else "knowledge_qa",
        "should_retrieve": bool(parsed.get("should_retrieve", True)),
        "standalone_query": standalone or query,
        "expanded_queries": expanded,
        "mentioned_kb_names": mentioned,
        "requires_multiple_kbs": bool(parsed.get("requires_multiple_kbs", False)),
    }


async def prepare_knowledge_context(
    *,
    query: str,
    llm_cfg: ResolvedModelConfig,
) -> dict[str, Any] | None:
    """Plan once, prefetch once, and return context for one final model call."""
    meta = get_process_request_meta()
    kb_ids = [str(value) for value in (meta.get("lcagent_knowledge_base_ids") or [])]
    kb_names = [str(value) for value in (meta.get("lcagent_knowledge_base_names") or [])]
    if not kb_ids or not query.strip():
        return None

    recent = meta.get("lcagent_recent_conversation")
    plan = await plan_knowledge_query(
        query=query,
        recent_conversation=recent if isinstance(recent, list) else [],
        kb_names=kb_names,
        llm_cfg=llm_cfg,
    )
    if not plan["should_retrieve"]:
        return {**plan, "has_evidence": False, "context_text": "", "evidence": []}

    base = str(meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base or not auth:
        logger.warning("Knowledge prefetch skipped: missing callback base or authorization")
        return {**plan, "has_evidence": False, "context_text": "", "evidence": []}

    payload = {
        "kb_ids": kb_ids,
        "original_query": query,
        "standalone_query": plan["standalone_query"],
        "expanded_queries": plan["expanded_queries"],
        "mentioned_kb_names": plan["mentioned_kb_names"],
        "requires_multiple_kbs": plan["requires_multiple_kbs"],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
            response = await client.post(
                f"{base}/console/api/kb/prefetch",
                json=payload,
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return {**plan, **result}
    except Exception:  # noqa: BLE001 - final answer still explains missing evidence
        logger.warning("Knowledge prefetch failed", exc_info=True)
    return {**plan, "has_evidence": False, "context_text": "", "evidence": []}
