# -*- coding: utf-8 -*-
"""Tests for LCAgent cost audit reporting from CoPaw."""
from __future__ import annotations

from copaw.token_usage.lcagent_cost_report import (
    _is_lcagent_home_context,
    _resolve_model_name,
)


def test_is_lcagent_home_context_with_resolved_llm():
    assert _is_lcagent_home_context({"lcagent_resolved_llm": {"model": "qwen-max"}})


def test_is_lcagent_home_context_without_meta():
    assert not _is_lcagent_home_context({})


def test_resolve_model_name_prefers_meta(monkeypatch):
    monkeypatch.setattr(
        "copaw.token_usage.lcagent_cost_report.get_process_request_meta",
        lambda: {"lcagent_resolved_llm": {"model": "kling-v2.6-pro-t2v"}},
    )
    assert _resolve_model_name("unknown") == "kling-v2.6-pro-t2v"
