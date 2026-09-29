# -*- coding: utf-8 -*-
"""首页 / IM 解析模型的输出上限（max_tokens）解析逻辑。

回归点：此前首页路径不传 max_tokens，长文输出会被云厂商默认上限
（DashScope Qwen ≈2000）静默截断、半截就结束。这里锁定优先级：
按模型下发值 > agent 运行配置默认 > 兜底常量。
"""
from types import SimpleNamespace

import copaw.config.config as config_module
from copaw.agents.model_factory import (
    _FALLBACK_MAX_OUTPUT_TOKENS,
    _resolve_resolved_max_output_tokens,
)
from copaw.providers.models import ResolvedModelConfig


def test_per_model_override_wins():
    """LCAgent 按模型下发的 max_output_tokens 优先，且无需加载 agent 配置。"""
    cfg = ResolvedModelConfig(model="qwen-plus", max_output_tokens=4096)

    assert _resolve_resolved_max_output_tokens(None, cfg) == 4096


def test_no_override_no_agent_id_uses_fallback():
    """既无按模型值、又无 agent_id 时，退回兜底常量（>= Qwen 默认，避免截断）。"""
    cfg = ResolvedModelConfig(model="qwen-plus")

    assert cfg.max_output_tokens is None
    resolved = _resolve_resolved_max_output_tokens(None, cfg)
    assert resolved == _FALLBACK_MAX_OUTPUT_TOKENS
    assert resolved >= 8192


def test_no_override_uses_running_config_default(monkeypatch):
    """无按模型值时，采用 agent 运行配置里的 max_output_tokens。"""
    monkeypatch.setattr(
        config_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            running=SimpleNamespace(max_output_tokens=6000),
        ),
    )
    cfg = ResolvedModelConfig(model="qwen-plus")

    assert _resolve_resolved_max_output_tokens("agent-x", cfg) == 6000


def test_running_config_failure_falls_back(monkeypatch):
    """加载 agent 配置抛错时不应崩溃，退回兜底常量。"""
    def _boom(agent_id):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(config_module, "load_agent_config", _boom)
    cfg = ResolvedModelConfig(model="qwen-plus")

    assert (
        _resolve_resolved_max_output_tokens("agent-x", cfg)
        == _FALLBACK_MAX_OUTPUT_TOKENS
    )


def test_non_positive_override_ignored(monkeypatch):
    """按模型值为 0 / 负数时视为未设置，继续走运行配置默认。"""
    monkeypatch.setattr(
        config_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            running=SimpleNamespace(max_output_tokens=8192),
        ),
    )
    cfg = ResolvedModelConfig.model_validate(
        {"model": "qwen-plus", "max_output_tokens": 0},
    )

    assert _resolve_resolved_max_output_tokens("agent-x", cfg) == 8192
