# -*- coding: utf-8 -*-
"""Tests for run_lcagent_workflow tool routing, binding guard and error mapping."""

# pylint: disable=redefined-outer-name
import json
from typing import Any, Optional

import pytest

import copaw.agents.tools.lcagent_app as lcagent_app
from copaw.agents.tools.lcagent_app import run_lcagent_workflow
from copaw import context as copaw_context


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self) -> Any:
        return self._body


class _FakeClient:
    """Captures requests and returns a scripted response."""

    requests: list[dict[str, Any]] = []
    response: Optional[_FakeResponse] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def request(self, method: str, url: str, json: Any = None, headers: Any = None):
        _FakeClient.requests.append(
            {"method": method, "url": url, "json": json, "headers": headers or {}}
        )
        assert _FakeClient.response is not None
        return _FakeClient.response


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _FakeClient.requests = []
    _FakeClient.response = _FakeResponse(202, {"runId": "run-1", "status": "running"})
    monkeypatch.setattr(lcagent_app.httpx, "Client", _FakeClient)
    copaw_context.set_process_request_meta(
        {
            "lcagent_console_api_base": "http://lcagent/console/api",
            "lcagent_workspace": {"type": "workflow", "id": "app-A", "revision": "rev-1"},
        }
    )
    copaw_context._request_authorization.set("Bearer token")
    yield
    copaw_context.set_process_request_meta(None)
    copaw_context._request_authorization.set(None)


def _tool_text(response) -> str:
    return response.content[0]["text"]


def test_bound_workspace_cannot_run_other_app():
    response = run_lcagent_workflow(action="start", app_id="app-B", scope="workflow")
    assert "WORKSPACE_BINDING_MISMATCH" in _tool_text(response)
    assert _FakeClient.requests == []


def test_start_posts_unified_run_payload():
    response = run_lcagent_workflow(
        action="start",
        scope="node",
        node_id="node-1",
        inputs_json='{"query": "hi"}',
    )
    req = _FakeClient.requests[0]
    assert req["method"] == "POST"
    assert req["url"].endswith("/console/api/workflow-runs")
    payload = req["json"]
    assert payload["workspace"] == {"type": "workflow", "id": "app-A"}
    assert payload["baseRevision"] == "rev-1"
    assert payload["scope"] == "node"
    assert payload["nodeId"] == "node-1"
    assert payload["inputs"] == {"query": "hi"}
    assert payload["idempotencyKey"]
    body = json.loads(_tool_text(response))
    assert body["kind"] == "lcagent_workflow_run"


def test_start_requires_base_revision_when_unbound():
    copaw_context.set_process_request_meta(
        {
            "lcagent_console_api_base": "http://lcagent/console/api",
            "lcagent_workspace": {"type": "workflow", "id": "app-A", "revision": ""},
        }
    )
    response = run_lcagent_workflow(action="start", scope="workflow")
    assert "REVISION_REQUIRED" in _tool_text(response)


def test_start_requires_scope():
    response = run_lcagent_workflow(action="start")
    assert "scope" in _tool_text(response)
    assert _FakeClient.requests == []


def test_start_rejects_invalid_inputs_json():
    response = run_lcagent_workflow(action="start", scope="workflow", inputs_json="{bad")
    assert "inputs_json" in _tool_text(response)


def test_get_run_routes_to_detail():
    run_lcagent_workflow(action="get_run", run_id="run-9")
    req = _FakeClient.requests[0]
    assert req["method"] == "GET"
    assert req["url"].endswith("/console/api/workflow-runs/run-9")


def test_get_events_includes_cursor_and_limit():
    run_lcagent_workflow(action="get_events", run_id="run-9", after_sequence=42)
    req = _FakeClient.requests[0]
    assert "afterSequence=42" in req["url"]
    assert "limit=200" in req["url"]


def test_get_node_result_requires_node_id():
    response = run_lcagent_workflow(action="get_node_result", run_id="run-9")
    assert "node_id" in _tool_text(response)
    assert _FakeClient.requests == []


def test_stop_and_resume_route_to_control_apis():
    run_lcagent_workflow(action="stop", run_id="run-9")
    run_lcagent_workflow(action="resume", run_id="run-9")
    assert _FakeClient.requests[0]["method"] == "POST"
    assert _FakeClient.requests[0]["url"].endswith("/workflow-runs/run-9/stop")
    assert _FakeClient.requests[1]["method"] == "POST"
    assert _FakeClient.requests[1]["url"].endswith("/workflow-runs/run-9/resume")


def test_http_error_maps_to_structured_error():
    _FakeClient.response = _FakeResponse(
        409,
        {"code": "REVISION_CONFLICT", "message": "stale revision"},
    )
    response = run_lcagent_workflow(action="start", scope="workflow")
    body = json.loads(_tool_text(response))
    assert body["ok"] is False
    assert body["httpStatus"] == 409
    assert body["error"]["code"] == "REVISION_CONFLICT"


def test_missing_base_url_fails_closed():
    copaw_context.set_process_request_meta({"lcagent_workspace": {"type": "workflow", "id": "app-A"}})
    response = run_lcagent_workflow(action="get_run", run_id="run-9")
    assert "lcagent_console_api_base" in _tool_text(response)
