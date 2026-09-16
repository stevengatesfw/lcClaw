"""Opt-in same-run RAG context event for offline evaluation."""

from __future__ import annotations

import copy
import json
from ..context import get_process_request_meta


class JsonStreamEvent(dict):
    """Adapter for AgentScope Runtime's ``model_dump_json`` SSE hook."""

    def model_dump_json(self) -> str:
        return json.dumps(self, ensure_ascii=False)


def same_run_rag_context_event() -> JsonStreamEvent | None:
    """Return the exact RAG context only for explicit evaluation requests."""
    meta = get_process_request_meta()
    if meta.get("lcagent_eval_capture_context") is not True:
        return None
    context = meta.get("lcagent_prefetched_kb_context")
    if not isinstance(context, dict):
        return None
    return JsonStreamEvent(
        {
            "event": "rag_context",
            "data": {
                key: copy.deepcopy(context.get(key))
                for key in (
                    "original_query",
                    "intent",
                    "source_scope",
                    "standalone_query",
                    "expanded_queries",
                    "queries",
                    "mentioned_kb_names",
                    "requires_multiple_kbs",
                    "selected_kb_ids",
                    "initial_kb_ids",
                    "searched_kb_ids",
                    "selected_knowledge_bases",
                    "cascade_expanded",
                    "routing",
                    "rerank_used",
                    "minimum_score",
                    "strong_score",
                    "evidence",
                    "context_text",
                    "has_evidence",
                    "evidence_status",
                    "evidence_signals",
                    "tool_policy",
                    "errors",
                )
                if key in context
            },
        },
    )
