# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Tests for tag_parser, focusing on Gemma 4 tool-call format support.

Gemma 4 uses asymmetric special tokens to delimit tool calls and a
``call:func_name{key:"value"}`` body format.  All token strings in these
tests are built from the module's own constants so the source stays free
of literal special-token sequences.
"""
from __future__ import annotations

from copaw.local_models.tag_parser import (
    GEMMA4_QUOTE_TOKEN,
    GEMMA4_TOOL_CALL_END,
    GEMMA4_TOOL_CALL_START,
    GEMMA4_TOOL_CALL_START_ALT,
    TOOL_CALL_END,
    TOOL_CALL_START,
    parse_tool_calls_from_text,
    strip_gemma_channel_markup,
    text_contains_tool_call_tag,
)


# --- Helpers ---------------------------------------------------------------

def _gemma_block(body: str, *, alt_open: bool = False) -> str:
    """Wrap *body* in Gemma 4 open/close tokens."""
    start = GEMMA4_TOOL_CALL_START_ALT if alt_open else GEMMA4_TOOL_CALL_START
    return start + body + GEMMA4_TOOL_CALL_END


def _q(value: str) -> str:
    """Wrap *value* in Gemma 4 quote tokens."""
    return GEMMA4_QUOTE_TOKEN + value + GEMMA4_QUOTE_TOKEN


# --- Gemma 4 native format -------------------------------------------------

def test_gemma4_native_single_string_arg() -> None:
    block = _gemma_block("call:search_knowledge_base{query:" + _q("知识库内容概览") + "}")
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "search_knowledge_base"
    assert tc.arguments == {"query": "知识库内容概览"}


def test_gemma4_cloud_open_tag_variant() -> None:
    """Cloud Gemma may use symmetric <|tool_call|> as the opening tag."""
    block = _gemma_block(
        "call:search_knowledge_base{query:" + _q("小明11的成绩") + "}",
        alt_open=True,
    )
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "search_knowledge_base"
    assert tc.arguments == {"query": "小明11的成绩"}


def test_gemma4_native_multiple_args() -> None:
    block = _gemma_block(
        "call:search_knowledge_base{query:" + _q("hello") + ", top_k:5}",
    )
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "search_knowledge_base"
    assert tc.arguments["query"] == "hello"
    assert tc.arguments["top_k"] == "5"


def test_gemma4_native_standard_quotes() -> None:
    block = _gemma_block('call:calculate{expression:"2+2"}')
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "calculate"
    assert tc.arguments == {"expression": "2+2"}


def test_gemma4_native_no_args() -> None:
    block = _gemma_block("call:get_time{}")
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "get_time"
    assert tc.arguments == {}


# --- Text around tool calls ------------------------------------------------

def test_gemma4_text_before_and_after() -> None:
    block = "Let me search. " + _gemma_block("call:find{pattern:test}") + " Done."
    result = parse_tool_calls_from_text(block)
    assert result.text_before == "Let me search."
    assert result.text_after == "Done."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "find"
    assert result.tool_calls[0].arguments == {"pattern": "test"}


def test_gemma4_multiple_blocks() -> None:
    text = (
        _gemma_block("call:foo{x:" + _q("1") + "}")
        + " middle "
        + _gemma_block("call:bar{y:" + _q("2") + "}")
    )
    result = parse_tool_calls_from_text(text)
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "foo"
    assert result.tool_calls[1].name == "bar"
    assert result.text_after == ""  # no trailing text after last block


# --- Streaming (unclosed tag) ----------------------------------------------

def test_gemma4_unclosed_tag_streaming() -> None:
    # Only the opening token, no close yet (mid-stream).
    partial = "thinking... " + GEMMA4_TOOL_CALL_START + "call:foo{x:"
    result = parse_tool_calls_from_text(partial)
    assert result.has_open_tag is True
    assert result.tool_calls == []
    assert result.text_before == "thinking..."
    assert result.partial_tool_text == "call:foo{x:"


# --- JSON fallback (some serving frameworks convert to JSON) ---------------

def test_gemma4_json_single_object_fallback() -> None:
    import json

    payload = json.dumps({"name": "get_weather", "arguments": {"city": "Beijing"}})
    block = _gemma_block(payload)
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "Beijing"}


def test_gemma4_json_array_fallback() -> None:
    import json

    payload = json.dumps(
        [
            {"name": "a", "arguments": {"k": "1"}},
            {"name": "b", "arguments": {"k": "2"}},
        ],
    )
    block = _gemma_block(payload)
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "a"
    assert result.tool_calls[1].name == "b"


# --- Standard format still works (regression) ------------------------------

def test_standard_json_format_still_works() -> None:
    import json

    payload = json.dumps({"name": "ping", "arguments": {"x": 1}})
    block = TOOL_CALL_START + payload + TOOL_CALL_END
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "ping"
    assert tc.arguments == {"x": 1}


def test_standard_xml_format_still_works() -> None:
    block = (
        TOOL_CALL_START
        + "<function=ping><parameter=x>1</parameter></function>"
        + TOOL_CALL_END
    )
    result = parse_tool_calls_from_text(block)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "ping"
    assert result.tool_calls[0].arguments == {"x": "1"}


# --- Mixed formats in one text --------------------------------------------

def test_mixed_standard_and_gemma4() -> None:
    import json

    std_payload = json.dumps({"name": "std_fn", "arguments": {"a": 1}})
    text = (
        TOOL_CALL_START + std_payload + TOOL_CALL_END
        + " "
        + _gemma_block("call:gemma_fn{b:" + _q("2") + "}")
    )
    result = parse_tool_calls_from_text(text)
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "std_fn"
    assert result.tool_calls[1].name == "gemma_fn"


# --- Tag detection ---------------------------------------------------------

def test_text_contains_tag_detects_gemma4() -> None:
    assert text_contains_tool_call_tag(GEMMA4_TOOL_CALL_START + "call:foo{}")
    assert text_contains_tool_call_tag("prefix " + _gemma_block("call:foo{}"))


def test_text_contains_tag_detects_cloud_gemma_open() -> None:
    assert text_contains_tool_call_tag(
        GEMMA4_TOOL_CALL_START_ALT + "call:search_knowledge_base{}",
    )


def test_text_contains_tag_detects_standard() -> None:
    assert text_contains_tool_call_tag(TOOL_CALL_START + "something")


def test_text_contains_tag_plain_text() -> None:
    assert not text_contains_tool_call_tag("just plain text, no tags here")


def test_plain_text_no_tool_calls() -> None:
    result = parse_tool_calls_from_text("Hello, how are you?")
    assert result.tool_calls == []
    assert result.text_before == "Hello, how are you?"
    assert result.has_open_tag is False


# --- Gemma channel markup stripping ----------------------------------------

def test_strip_gemma_channel_thinking_noise() -> None:
    raw = "<|channel|>thought\n<channel|><|channel|>thought\n<channel|>"
    assert strip_gemma_channel_markup(raw) == ""


def test_strip_gemma_channel_answer_prefix() -> None:
    raw = "<channel|>小明11的成绩如下：\n语文：122"
    assert strip_gemma_channel_markup(raw) == "小明11的成绩如下：\n语文：122"


def test_strip_gemma_channel_plain_text_unchanged() -> None:
    raw = "小明12的成绩如下：\n语文：120"
    assert strip_gemma_channel_markup(raw) == raw
