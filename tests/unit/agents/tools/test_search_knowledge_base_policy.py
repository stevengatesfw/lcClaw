import json
import importlib

import pytest

from copaw import context as copaw_context
search_module = importlib.import_module('copaw.agents.tools.search_knowledge_base')


class _Response:
    status_code = 200
    text = '[]'

    @staticmethod
    def json():
        return []


class _Client:
    calls = 0

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, *_args, **_kwargs):
        _Client.calls += 1
        return _Response()


@pytest.fixture(autouse=True)
def _context(monkeypatch):
    _Client.calls = 0
    monkeypatch.setattr(search_module.httpx, 'Client', _Client)
    copaw_context.set_request_authorization('Bearer token')
    yield
    copaw_context.reset_process_request_meta()
    copaw_context.reset_request_authorization()


def _text(response):
    return response.content[0]['text']


def _meta(allow: bool):
    copaw_context.set_process_request_meta({
        'lcagent_console_api_base': 'http://lcagent',
        'lcagent_knowledge_base_count': 1,
        'lcagent_knowledge_base_ids': ['kb-1'],
        'lcagent_tool_policy': {
            'allow_kb_fallback': allow,
            'kb_fallback_max_calls': 1,
        },
    })


def test_policy_denies_unplanned_kb_tool_call():
    _meta(False)

    response = search_module.search_knowledge_base('问题')

    assert '不允许' in _text(response)
    assert _Client.calls == 0


def test_technical_failure_allows_only_one_bounded_fallback():
    _meta(True)

    first = search_module.search_knowledge_base('问题', top_k=100)
    second = search_module.search_knowledge_base('问题')

    assert json.loads(_Response.text) == []
    assert '未找到相关内容' in _text(first)
    assert '次数已用完' in _text(second)
    assert _Client.calls == 1
