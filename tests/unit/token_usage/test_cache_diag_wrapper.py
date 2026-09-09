# -*- coding: utf-8 -*-
"""链路 A（copaw）P0 缓存可观测性测试。

覆盖：
- TokenRecordingModelWrapper 的 cache_diag 打点（非流式 / 流式）；
- normalize_cache_usage 解析 ChatUsage.metadata 中的 provider 原始 usage；
- TokenUsageManager 的 cached_tokens 聚合与旧文件兼容；
- session contextvar 的设置/读取/清理。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import copaw.token_usage.model_wrapper as mw_module
from copaw.token_usage import cache_diagnostics as cd
from copaw.token_usage.manager import TokenUsageManager
from copaw.token_usage.model_wrapper import TokenRecordingModelWrapper


@pytest.fixture(autouse=True)
def _clear_diag_store():
    cd._store.clear()
    yield
    cd._store.clear()


class _FakeLogger:
    def __init__(self):
        self.records = []

    def log(self, level, msg):
        self.records.append((level, msg))

    def debug(self, msg):
        self.records.append((logging.DEBUG, msg))

    def isEnabledFor(self, level):
        return True

    @property
    def info_lines(self):
        return [m for lvl, m in self.records if lvl == logging.INFO]


class _FakeUsageManager:
    def __init__(self):
        self.calls = []

    async def record(self, **kwargs):
        self.calls.append(kwargs)


def _chat_usage(input_tokens, output_tokens, raw_metadata=None):
    """模拟 agentscope ChatUsage（dataclass，metadata 持有 provider 原始 usage）。"""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        time=0.5,
        type="chat",
        metadata=raw_metadata,
    )


class _FakeModel:
    """模拟 agentscope ChatModelBase：非流式返回对象 / 流式返回 async generator。"""

    model_name = "qwen3.8-max"
    stream = True

    def __init__(self, response):
        self.client = SimpleNamespace(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.calls = []
        self._response = response

    async def __call__(self, messages, tools=None, tool_choice=None,
                       structured_model=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        return self._response


async def _agen(items):
    for it in items:
        yield it


def _make_wrapper(response, monkeypatch, session=None):
    model = _FakeModel(response)
    wrapper = TokenRecordingModelWrapper("dashscope", model)
    fake_mgr = _FakeUsageManager()
    fake_log = _FakeLogger()
    monkeypatch.setattr(mw_module, "get_token_usage_manager", lambda: fake_mgr)
    monkeypatch.setattr(mw_module, "logger", fake_log)
    if session is not None:
        from copaw.context import set_current_session_id

        set_current_session_id(session)
    return wrapper, model, fake_mgr, fake_log


@pytest.fixture
def _reset_session():
    yield
    from copaw.context import reset_current_session_id

    reset_current_session_id()


# ---------------------------------------------------------------------------
# 双胞胎模块在 lcClaw 环境下的基本行为
# ---------------------------------------------------------------------------

def test_normalize_usage_variants():
    u = cd.normalize_cache_usage({
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 900},
        "completion_tokens": 20,
    })
    assert u["cached_tokens"] == 900 and u["cache_miss_tokens"] == 100
    u2 = cd.normalize_cache_usage({
        "prompt_tokens": 10, "prompt_cache_hit_tokens": 4,
    })
    assert u2["cached_tokens"] == 4 and u2["usage_source"] == "prompt_cache_hit_tokens"
    assert cd.normalize_cache_usage(None)["cached_tokens"] is None


# ---------------------------------------------------------------------------
# wrapper 打点
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_reset_session")
async def test_wrapper_non_stream_logs_cache_diag(monkeypatch):
    raw = {
        "prompt_tokens": 300,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 256},
    }
    response = SimpleNamespace(usage=_chat_usage(300, 30, raw))
    wrapper, model, mgr, log = _make_wrapper(response, monkeypatch, session="sess-1")

    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q1"}]
    await wrapper(messages=msgs, tools=[{"name": "t1"}])

    # 单行 cache_diag：chain=A、cached 与 shared_prefix 同行、endpoint/model 可见
    lines = log.info_lines
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("cache_diag chain=A ")
    assert "session=sess-1" in line
    assert "provider=dashscope" in line
    assert "model=qwen3.8-max" in line
    assert "endpoint=https://dashscope.aliyuncs.com/compatible-mode/v1" in line
    assert "input=300" in line and "cached=256" in line and "hit=85.3%" in line
    assert "prev=n" in line and "first_diff=first" in line

    # usage 落库携带 cached_tokens
    assert mgr.calls == [dict(
        provider_id="dashscope", model_name="qwen3.8-max",
        prompt_tokens=300, completion_tokens=30, cached_tokens=256,
        at_date=mgr.calls[0]["at_date"],
    )]

    # 第二轮：追加 assistant+user 两条 → prev=y，首个新增消息在 index 2，
    # first_diff=turn[2]，共享前缀（system+tools+前两条消息）> 0
    msgs2 = msgs + [{"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"}]
    await wrapper(messages=msgs2, tools=[{"name": "t1"}])
    line2 = log.info_lines[-1]
    assert "prev=y" in line2 and "first_diff=turn[2]" in line2
    assert "shared_prefix≈" in line2 and "shared_prefix≈0" not in line2

    # 第三轮：只追加本轮 user 一条 → first_diff=dynamic（健康追加形态）
    msgs3 = msgs2 + [{"role": "user", "content": "q3"}]
    await wrapper(messages=msgs3, tools=[{"name": "t1"}])
    assert "first_diff=dynamic" in log.info_lines[-1]


@pytest.mark.usefixtures("_reset_session")
async def test_wrapper_stream_logs_cache_diag(monkeypatch):
    chunks = [
        SimpleNamespace(usage=None),
        SimpleNamespace(usage=_chat_usage(
            200, 15,
            {"prompt_tokens": 200, "completion_tokens": 15,
             "prompt_cache_hit_tokens": 128, "prompt_cache_miss_tokens": 72},
        )),
    ]
    wrapper, model, mgr, log = _make_wrapper(_agen(chunks), monkeypatch, session="sess-2")

    result = await wrapper(messages=[{"role": "user", "content": "hi"}])
    got = [c async for c in result]
    assert len(got) == 2

    line = log.info_lines[-1]
    assert line.startswith("cache_diag chain=A ")
    assert "cached=128" in line and "miss=72" in line
    assert "usage_src=prompt_cache_hit_tokens" in line
    assert mgr.calls and mgr.calls[-1]["cached_tokens"] == 128


@pytest.mark.usefixtures("_reset_session")
async def test_wrapper_without_session_context_falls_back(monkeypatch):
    response = SimpleNamespace(usage=_chat_usage(10, 2, {"prompt_tokens": 10}))
    wrapper, model, mgr, log = _make_wrapper(response, monkeypatch, session=None)
    await wrapper(messages=[{"role": "user", "content": "q"}])
    assert "session=copaw:dashscope:qwen3.8-max" in log.info_lines[-1]


@pytest.mark.usefixtures("_reset_session")
async def test_wrapper_diagnostics_never_break_call(monkeypatch):
    # 打点内部异常不得影响模型调用与 usage 记录
    response = SimpleNamespace(usage=_chat_usage(10, 2, None))
    wrapper, model, mgr, log = _make_wrapper(response, monkeypatch, session="s")

    def _boom(*a, **k):
        raise RuntimeError("diag boom")

    monkeypatch.setattr(mw_module, "begin_request_diagnostics", _boom)
    out = await wrapper(messages=[{"role": "user", "content": "q"}])
    assert out is response
    assert mgr.calls and mgr.calls[-1]["cached_tokens"] == 0


async def test_wrapper_usage_none_no_record(monkeypatch):
    response = SimpleNamespace(usage=None)
    wrapper, model, mgr, log = _make_wrapper(response, monkeypatch, session="s")
    await wrapper(messages=[{"role": "user", "content": "q"}])
    assert mgr.calls == []


# ---------------------------------------------------------------------------
# TokenUsageManager cached_tokens 聚合
# ---------------------------------------------------------------------------

async def test_manager_records_and_aggregates_cached(tmp_path):
    mgr = TokenUsageManager(tmp_path / "token_usage.json")
    await mgr.record("dashscope", "qwen3.8-max", 100, 10, cached_tokens=80)
    await mgr.record("dashscope", "qwen3.8-max", 50, 5, cached_tokens=20)
    summary = await mgr.get_summary()
    assert summary.total_prompt_tokens == 150
    assert summary.total_cached_tokens == 100
    key = "dashscope:qwen3.8-max"
    assert summary.by_model[key].cached_tokens == 100
    assert summary.by_provider["dashscope"].cached_tokens == 100


async def test_manager_backward_compatible_with_old_file(tmp_path):
    # 旧格式文件（无 cached_tokens 键）必须可以继续累计并被 summary 读取
    path = tmp_path / "token_usage.json"
    from datetime import date

    legacy = {
        date.today().isoformat(): {
            "dashscope:qwen3.8-max": {
                "provider_id": "dashscope",
                "model_name": "qwen3.8-max",
                "prompt_tokens": 111,
                "completion_tokens": 22,
                "call_count": 3,
            },
        },
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    mgr = TokenUsageManager(path)
    await mgr.record("dashscope", "qwen3.8-max", 10, 1, cached_tokens=5)
    summary = await mgr.get_summary()
    assert summary.total_prompt_tokens == 121
    assert summary.total_cached_tokens == 5


# ---------------------------------------------------------------------------
# session contextvar
# ---------------------------------------------------------------------------

def test_session_contextvar_roundtrip():
    from copaw.context import (
        get_context_session_id,
        reset_current_session_id,
        set_current_session_id,
    )

    assert get_context_session_id() is None
    set_current_session_id("sess-xyz")
    assert get_context_session_id() == "sess-xyz"
    reset_current_session_id()
    assert get_context_session_id() is None
