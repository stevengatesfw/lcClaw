# -*- coding: utf-8 -*-
"""Tests for manage_lcagent_workflow layered catalog routing and size guards."""

# pylint: disable=redefined-outer-name
import json
from typing import Any, ClassVar

import pytest
from typing_extensions import Self

from copaw import context as copaw_context
from copaw.agents.tools import lcagent_app
from copaw.agents.tools.lcagent_app import manage_lcagent_workflow
from copaw.config.config import ToolResultCompactConfig
from copaw.constant import TRUNCATION_NOTICE_MARKER


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self) -> Any:
        return self._body


class _FakeClient:
    """Captures requests and returns a scripted response."""

    requests: ClassVar[list[dict[str, Any]]] = []
    response: ClassVar[_FakeResponse | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
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
    _FakeClient.response = _FakeResponse(200, {"schemaVersion": 2, "view": "index"})
    monkeypatch.setattr(lcagent_app.httpx, "Client", _FakeClient)
    copaw_context.set_process_request_meta(
        {
            "lcagent_console_api_base": "http://lcagent",
            "lcagent_workspace": {
                "type": "workflow",
                "id": "app-A",
                "revision": "rev-1",
            },
        }
    )
    copaw_context._request_authorization.set("Bearer token")
    yield
    copaw_context.set_process_request_meta(None)
    copaw_context._request_authorization.set(None)


def _tool_text(response) -> str:
    return response.content[0]["text"]


def test_catalog_requests_index_view():
    manage_lcagent_workflow(action="catalog")
    req = _FakeClient.requests[0]
    assert req["method"] == "GET"
    assert req["url"].endswith("/console/api/workflow-editing/catalog?view=index")


def test_get_component_schema_routes_to_component_view():
    manage_lcagent_workflow(
        action="get_component_schema", component_type="llm-text-generation"
    )
    req = _FakeClient.requests[0]
    assert req["method"] == "GET"
    assert "view=component" in req["url"]
    assert "node_type=llm-text-generation" in req["url"]


def test_get_component_schema_requires_component_type():
    response = manage_lcagent_workflow(action="get_component_schema")
    assert "component_type" in _tool_text(response)
    assert _FakeClient.requests == []


def test_get_model_detail_routes_to_model_view():
    manage_lcagent_workflow(action="get_model_detail", resource_id="12:34")
    req = _FakeClient.requests[0]
    assert "view=model" in req["url"]
    assert "id=12%3A34" in req["url"]


def test_get_model_detail_requires_resource_id():
    response = manage_lcagent_workflow(action="get_model_detail")
    assert "resource_id" in _tool_text(response)
    assert _FakeClient.requests == []


def test_get_mcp_tools_routes_to_mcp_view():
    manage_lcagent_workflow(action="get_mcp_tools", resource_id="7")
    req = _FakeClient.requests[0]
    assert "view=mcp" in req["url"]
    assert "id=7" in req["url"]


def test_get_mcp_tools_requires_resource_id():
    response = manage_lcagent_workflow(action="get_mcp_tools")
    assert "resource_id" in _tool_text(response)
    assert _FakeClient.requests == []


def test_validate_posts_summary_view_and_wraps_change_set():
    _FakeClient.response = _FakeResponse(
        200,
        {
            "id": "cs-1",
            "status": "pending",
            "target": {"mode": "existing", "appId": "app-A", "name": None},
            "diff": [{"type": "layout_updated"}],
            "diffTotal": 1,
            "diffTruncated": False,
        },
    )
    response = manage_lcagent_workflow(
        action="validate",
        patch_json=json.dumps(
            {"summary": "s", "operations": [{"type": "layout_graph"}]}
        ),
    )
    req = _FakeClient.requests[0]
    assert req["method"] == "POST"
    assert req["url"].endswith(
        "/console/api/workflow-editing/change-sets/validate?view=summary"
    )
    assert req["json"]["workspace"] == {"type": "workflow", "id": "app-A"}
    assert req["json"]["baseRevision"] == "rev-1"
    body = json.loads(_tool_text(response))
    assert body["kind"] == "lcagent_change_set"
    assert body["requiresUserConfirmation"] is True
    assert body["diffTotal"] == 1


def test_get_change_set_requests_summary_and_maps_creation_state():
    _FakeClient.response = _FakeResponse(
        200,
        {
            "id": "cs-1",
            "status": "applied",
            "target": {"mode": "create", "appId": "app-9", "name": "n"},
            "diff": [],
        },
    )
    response = manage_lcagent_workflow(action="get_change_set", change_set_id="cs-1")
    req = _FakeClient.requests[0]
    assert req["method"] == "GET"
    assert req["url"].endswith(
        "/console/api/workflow-editing/change-sets/cs-1?view=summary"
    )
    body = json.loads(_tool_text(response))
    assert body["creationState"] == "created"


def test_bound_workspace_cannot_read_other_app_context():
    response = manage_lcagent_workflow(action="context", app_id="app-B")
    assert "WORKSPACE_BINDING_MISMATCH" in _tool_text(response)
    assert _FakeClient.requests == []


def test_tool_text_marks_oversized_payload():
    response = lcagent_app._lcagent_tool_text("x" * (30 * 1024))
    text = _tool_text(response)
    assert TRUNCATION_NOTICE_MARKER in text
    assert len(text.encode("utf-8")) < 30 * 1024


def test_tool_text_keeps_small_payload_intact():
    payload = json.dumps({"ok": True})
    response = lcagent_app._lcagent_tool_text(payload)
    assert _tool_text(response) == payload


def test_compaction_threshold_stays_above_tool_budget():
    """Memory compaction must not cut structured payloads before the tool's own guard."""
    assert (
        ToolResultCompactConfig().old_max_bytes
        > lcagent_app._LCAGENT_TOOL_MAX_BYTES
    )
