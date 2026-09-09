# -*- coding: utf-8 -*-
"""LCAgent billing callback must carry prefix-cache hits.

``cache_diag`` on the homepage assistant (chain A) routinely reports 92%~99%
prefix-cache hit rates, but ``cost_audits.prompt_cached_tokens`` used to stay
at 0 for every ``call_type='lcclaw'`` row because this payload omitted the
field — even though ``LcclawTokenReportApi`` accepts it and
``TokenUsageSummary`` already aggregates ``total_cached_tokens``.
"""

from types import SimpleNamespace

import pytest

from copaw.app.runner import lcagent_token_report as ltr


@pytest.fixture(autouse=True)
def _report_secret(monkeypatch):
    monkeypatch.setenv("LCAGENT_TOKEN_REPORT_SECRET", "test-secret")


@pytest.fixture
def captured(monkeypatch):
    """Replace httpx with a fake client that records the outgoing payload."""
    seen = {}

    class _FakeResponse:
        status_code = 200
        text = ""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(
        ltr,
        "httpx",
        SimpleNamespace(AsyncClient=_FakeClient, Timeout=lambda *a, **kw: None),
    )
    return seen


async def test_payload_carries_prompt_cached_tokens(captured):
    await ltr.report_tokens_after_run(
        console_api_base="http://api:5000/",
        user_id="user-1",
        prompt_tokens=37995,
        completion_tokens=412,
        session_id="sess-1",
        tenant_id="tenant-1",
        model_name="qwen3.8-flash",
        prompt_cached_tokens=34944,
    )

    assert captured["url"] == (
        "http://api:5000/console/api/costaudit/lcclaw_token_report"
    )
    payload = captured["json"]
    assert payload["prompt_cached_tokens"] == 34944
    assert payload["prompt_tokens"] == 37995
    assert payload["completion_tokens"] == 412
    assert payload["session_id"] == "sess-1"
    assert payload["lcagent_tenant_id"] == "tenant-1"
    assert payload["model_name"] == "qwen3.8-flash"
    assert captured["headers"]["Authorization"] == "Bearer test-secret"


async def test_cached_defaults_to_zero_for_old_callers(captured):
    await ltr.report_tokens_after_run(
        console_api_base="http://api:5000",
        user_id="user-1",
        prompt_tokens=100,
        completion_tokens=10,
    )

    assert captured["json"]["prompt_cached_tokens"] == 0


@pytest.mark.parametrize("cached", [-5, None])
async def test_non_positive_cached_is_clamped(captured, cached):
    await ltr.report_tokens_after_run(
        console_api_base="http://api:5000",
        user_id="user-1",
        prompt_tokens=100,
        completion_tokens=10,
        prompt_cached_tokens=cached,
    )

    assert captured["json"]["prompt_cached_tokens"] == 0


async def test_cached_only_does_not_trigger_report(captured):
    """A cache hit without any token delta is still not billable."""
    await ltr.report_tokens_after_run(
        console_api_base="http://api:5000",
        user_id="user-1",
        prompt_tokens=0,
        completion_tokens=0,
        prompt_cached_tokens=4096,
    )

    assert captured == {}


async def test_missing_secret_skips_report(captured, monkeypatch):
    monkeypatch.setenv("LCAGENT_TOKEN_REPORT_SECRET", "")

    await ltr.report_tokens_after_run(
        console_api_base="http://api:5000",
        user_id="user-1",
        prompt_tokens=100,
        completion_tokens=10,
        prompt_cached_tokens=80,
    )

    assert captured == {}
