# -*- coding: utf-8 -*-
"""Request-scoped limits for the LCAgent homepage assistant."""

from copaw.app.runner.runner import (
    _LCAGENT_HOME_MAX_ITERS,
    _limit_lcagent_home_max_iters,
)
from copaw.config.config import AgentProfileConfig, AgentsRunningConfig
from copaw.context import reset_process_request_meta, set_process_request_meta


def _agent_config(max_iters: int = 100) -> AgentProfileConfig:
    return AgentProfileConfig(
        id="default",
        name="Default",
        running=AgentsRunningConfig(max_iters=max_iters),
    )


def test_lcagent_homepage_caps_react_iterations_without_mutating_config():
    config = _agent_config()
    set_process_request_meta({"lcagent_console_api_base": "http://api:5000"})
    try:
        limited = _limit_lcagent_home_max_iters(config, "console")
    finally:
        reset_process_request_meta()

    assert limited is not config
    assert limited.running.max_iters == _LCAGENT_HOME_MAX_ITERS
    assert config.running.max_iters == 100


def test_non_lcagent_or_non_console_requests_keep_configured_iterations():
    config = _agent_config()

    set_process_request_meta({})
    try:
        assert _limit_lcagent_home_max_iters(config, "console") is config
    finally:
        reset_process_request_meta()

    set_process_request_meta({"lcagent_console_api_base": "http://api:5000"})
    try:
        assert _limit_lcagent_home_max_iters(config, "dingtalk") is config
    finally:
        reset_process_request_meta()


def test_lcagent_homepage_keeps_stricter_saved_limit():
    config = _agent_config(max_iters=8)
    set_process_request_meta({"lcagent_console_api_base": "http://api:5000"})
    try:
        assert _limit_lcagent_home_max_iters(config, "console") is config
    finally:
        reset_process_request_meta()

    assert config.running.max_iters == 8
