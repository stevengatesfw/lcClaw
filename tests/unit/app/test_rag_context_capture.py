import json

from copaw.app.rag_context_capture import same_run_rag_context_event
from copaw.context import reset_process_request_meta, set_process_request_meta


def teardown_function():
    reset_process_request_meta()


def test_capture_is_disabled_by_default():
    set_process_request_meta(
        {"lcagent_prefetched_kb_context": {"evidence": [{"content": "C1"}]}}
    )

    assert same_run_rag_context_event() is None


def test_capture_serializes_exact_prefetched_context():
    context = {
        "standalone_query": "改写问题",
        "expanded_queries": ["扩展问题"],
        "selected_kb_ids": ["kb-1"],
        "evidence": [{"content": "精确上下文 C1", "score": 0.9}],
        "context_text": "[资料 1]\n内容：\n精确上下文 C1",
        "has_evidence": True,
    }
    set_process_request_meta(
        {
            "lcagent_eval_capture_context": True,
            "lcagent_prefetched_kb_context": context,
        }
    )

    event = same_run_rag_context_event()
    payload = json.loads(event.model_dump_json())

    assert payload["event"] == "rag_context"
    assert payload["data"] == context
