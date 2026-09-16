# -*- coding: utf-8 -*-
"""Request planning and server-controlled retrieval for home chat."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .model_factory import create_model_and_formatter
from ..context import get_process_request_meta, get_request_authorization
from ..providers.models import ResolvedModelConfig

logger = logging.getLogger(__name__)

_MAX_RECENT_MESSAGES = 6
_MAX_RECENT_CONTENT = 2000
_INTENTS = {
    "knowledge_meta",
    "knowledge_qa",
    "memory_query",
    "general_tool_task",
}
_SOURCE_SCOPES = {"selected_kb_only", "open"}


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


def _protected_query_terms(query: str) -> list[str]:
    patterns = (
        r"[A-Za-z][A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*",
        r"\d{2,4}(?:[-/.年]\d{1,2})?(?:[-/.月]\d{1,2})?日?",
        r"[《「『\"']([^》」』\"']+)[》」』\"']",
        r"不得|不能|不可|禁止|不要|未|无|不",
    )
    terms: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, query, flags=re.IGNORECASE):
            value = match.group(1) if match.lastindex else match.group(0)
            if value and value not in terms:
                terms.append(value)
    return [
        term
        for term in terms
        if not any(
            term != other and term.lower() in other.lower()
            for other in terms
        )
    ]


def _sanitize_rewrites(
    original_query: str,
    standalone_query: str,
    expanded_queries: list[str],
) -> tuple[str, list[str]]:
    protected = _protected_query_terms(original_query)
    standalone = standalone_query
    if any(term.lower() not in standalone.lower() for term in protected):
        standalone = original_query

    original_numbered = {
        value.lower()
        for value in re.findall(r"[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*", original_query)
    }
    safe_expanded: list[str] = []
    for expanded in expanded_queries:
        introduced = {
            value.lower()
            for value in re.findall(r"[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*", expanded)
        } - original_numbered
        if introduced:
            continue
        missing = [term for term in protected if term.lower() not in expanded.lower()]
        safe_expanded.append(
            f"{expanded} {' '.join(missing)}".strip() if missing else expanded,
        )
    return standalone, safe_expanded


def _fallback_plan(query: str, *, has_selected_kbs: bool) -> dict[str, Any]:
    normalized = re.sub(r"\s+", "", query)
    selected_only = bool(
        re.search(r"(?:仅|只)(?:能|可|需|要)?(?:根据|依据|使用|查|从).{0,8}知识库", normalized)
        or re.search(r"答案.{0,5}(?:仅|只).{0,8}知识库", normalized)
    )
    if (
        re.search(r"(?:选了|选择了|当前|已选).{0,8}(?:哪些|什么|几个|多少).{0,4}知识库", normalized)
        or re.search(r"(?:有哪些|什么|几个|多少)(?:已选)?知识库", normalized)
        or re.search(r"知识库(?:名称|清单|列表|ID)", normalized, flags=re.IGNORECASE)
    ):
        intent = "knowledge_meta"
    elif re.search(r"(?:之前|过去|历史).{0,8}(?:说过|讨论|决定|决策|对话)", normalized) or re.search(
        r"(?:我的|用户).{0,5}(?:偏好|习惯|待办|记忆)",
        normalized,
    ):
        intent = "memory_query"
    elif has_selected_kbs:
        intent = "knowledge_qa"
    else:
        intent = "general_tool_task"
    return {
        "intent": intent,
        "source_scope": "selected_kb_only" if selected_only else "open",
        "standalone_query": query,
        "expanded_queries": [],
        "mentioned_kb_names": [],
        "requires_multiple_kbs": False,
    }


async def plan_request(
    *,
    query: str,
    recent_conversation: list[dict[str, str]],
    kb_names: list[str],
    llm_cfg: ResolvedModelConfig,
) -> dict[str, Any]:
    """Classify source intent and create retrieval-only rewrites."""
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
    fallback = _fallback_plan(query, has_selected_kbs=bool(kb_names))
    planner_cfg = llm_cfg.model_copy(update={"enable_thinking": False})
    model, _ = create_model_and_formatter(llm_cfg=planner_cfg)
    system = """你是请求路由与知识检索规划器。只输出一个 JSON 对象，不解释，也不要回答问题。
结合最近对话，把当前追问改写为语义完整的独立问题；改写只用于检索，最终回答仍使用原始问题。

intent 必须为以下之一：
- knowledge_meta：询问当前已选知识库的名称、数量或 ID，不查询文档。
- knowledge_qa：询问已选知识库中的文档内容。
- memory_query：明确询问历史对话、用户偏好、过去决策或待办。
- general_tool_task：其他普通问答或工具任务。

source_scope 必须为以下之一：
- selected_kb_only：用户明确要求只依据、只使用已选知识库。
- open：用户没有限制信息来源。

JSON 字段：intent、source_scope、standalone_query、expanded_queries、mentioned_kb_names、requires_multiple_kbs。
expanded_queries 最多两条，只能补充同义词、业务术语和关键词组合。
standalone_query 和 expanded_queries 必须原样保留名称、数字、编号、型号、时间与否定条件，不得引入新事实。
mentioned_kb_names 只能取自 selected_knowledge_bases。"""
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
    except Exception:  # noqa: BLE001 - deterministic routing fallback remains available
        logger.warning("Request planning failed", exc_info=True)
        return fallback

    intent = str(parsed.get("intent") or fallback["intent"])
    source_scope = str(parsed.get("source_scope") or fallback["source_scope"])
    standalone = str(parsed.get("standalone_query") or query).strip()[:4000]
    expanded = [
        str(item).strip()[:1000]
        for item in (parsed.get("expanded_queries") or [])[:2]
        if str(item).strip()
    ]
    standalone, expanded = _sanitize_rewrites(query, standalone, expanded)
    mentioned = [
        str(item).strip()[:200]
        for item in (parsed.get("mentioned_kb_names") or [])[:5]
        if str(item).strip() and str(item).strip() in kb_names
    ]
    return {
        "intent": intent if intent in _INTENTS else fallback["intent"],
        "source_scope": (
            source_scope if source_scope in _SOURCE_SCOPES else fallback["source_scope"]
        ),
        "standalone_query": standalone or query,
        "expanded_queries": expanded,
        "mentioned_kb_names": mentioned,
        "requires_multiple_kbs": bool(parsed.get("requires_multiple_kbs", False)),
    }


def build_tool_policy(
    *,
    intent: str,
    source_scope: str,
    evidence_status: str,
    enable_agent: bool,
    enable_skills: bool,
) -> dict[str, Any]:
    """Return the sole authority used by prompt and actual tool registration."""
    policy = {
        "allow_general_tools": False,
        "allow_skills": False,
        "allow_memory_search": False,
        "allow_kb_fallback": False,
        "kb_fallback_max_calls": 0,
    }
    if intent == "memory_query":
        policy["allow_memory_search"] = True
    elif intent == "general_tool_task":
        policy["allow_general_tools"] = bool(enable_agent or enable_skills)
        policy["allow_skills"] = bool(enable_skills)
    elif intent == "knowledge_qa" and evidence_status in {"no_evidence", "retrieval_error"}:
        if source_scope == "open":
            policy["allow_general_tools"] = bool(enable_agent or enable_skills)
            policy["allow_skills"] = bool(enable_skills)
        if evidence_status == "retrieval_error":
            policy["allow_kb_fallback"] = True
            policy["kb_fallback_max_calls"] = 1
    return policy


def _metadata_result(plan: dict[str, Any], kb_ids: list[str], kb_names: list[str]) -> dict[str, Any]:
    selected = [
        {"kb_id": kb_id, "kb_name": kb_names[index] if index < len(kb_names) else ""}
        for index, kb_id in enumerate(kb_ids)
    ]
    return {
        **plan,
        "evidence_status": "metadata_answer",
        "selected_knowledge_bases": selected,
        "has_evidence": False,
        "context_text": "",
        "evidence": [],
        "errors": [],
    }


async def prepare_request_context(
    *,
    query: str,
    llm_cfg: ResolvedModelConfig,
    enable_agent: bool = False,
    enable_skills: bool = False,
) -> dict[str, Any]:
    """Plan every home request and prefetch KB evidence only when appropriate."""
    meta = get_process_request_meta()
    kb_ids = [str(value) for value in (meta.get("lcagent_knowledge_base_ids") or [])]
    kb_names = [str(value) for value in (meta.get("lcagent_knowledge_base_names") or [])]
    recent = meta.get("lcagent_recent_conversation")
    plan = await plan_request(
        query=query,
        recent_conversation=recent if isinstance(recent, list) else [],
        kb_names=kb_names,
        llm_cfg=llm_cfg,
    )

    selected_metadata = [
        {"kb_id": kb_id, "kb_name": kb_names[index] if index < len(kb_names) else ""}
        for index, kb_id in enumerate(kb_ids)
    ]
    if plan["intent"] == "knowledge_meta":
        result = _metadata_result(plan, kb_ids, kb_names)
    elif plan["intent"] != "knowledge_qa" or not kb_ids:
        result = {
            **plan,
            "evidence_status": "no_evidence",
            "selected_knowledge_bases": selected_metadata,
            "has_evidence": False,
            "context_text": "",
            "evidence": [],
            "errors": [],
        }
    else:
        base = str(meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
        auth = get_request_authorization().strip()
        if not base or not auth:
            logger.warning("Knowledge prefetch skipped: missing callback base or authorization")
            result = {
                **plan,
                "evidence_status": "retrieval_error",
                "selected_knowledge_bases": selected_metadata,
                "has_evidence": False,
                "context_text": "",
                "evidence": [],
                "errors": ["prefetch:MissingCallbackConfiguration"],
            }
        else:
            payload = {
                "kb_ids": kb_ids,
                "original_query": query,
                "standalone_query": plan["standalone_query"],
                "expanded_queries": plan["expanded_queries"],
                "mentioned_kb_names": plan["mentioned_kb_names"],
                "requires_multiple_kbs": plan["requires_multiple_kbs"],
            }
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(90.0, connect=15.0),
                ) as client:
                    response = await client.post(
                        f"{base}/console/api/kb/prefetch",
                        json=payload,
                        headers={"Authorization": auth, "Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("invalid prefetch response")
                    result = {**plan, "selected_knowledge_bases": selected_metadata, **body}
            except Exception as exc:  # noqa: BLE001 - policy handles the explicit failure
                logger.warning("Knowledge prefetch failed", exc_info=True)
                result = {
                    **plan,
                    "evidence_status": "retrieval_error",
                    "selected_knowledge_bases": selected_metadata,
                    "has_evidence": False,
                    "context_text": "",
                    "evidence": [],
                    "errors": [f"prefetch:{type(exc).__name__}"],
                }

    result["tool_policy"] = build_tool_policy(
        intent=str(result.get("intent") or "general_tool_task"),
        source_scope=str(result.get("source_scope") or "open"),
        evidence_status=str(result.get("evidence_status") or "no_evidence"),
        enable_agent=enable_agent,
        enable_skills=enable_skills,
    )
    return result


# Compatibility alias for external callers that already imported this symbol.
prepare_knowledge_context = prepare_request_context
