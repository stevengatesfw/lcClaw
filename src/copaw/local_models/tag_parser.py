# -*- coding: utf-8 -*-
"""Parse special tags from model-generated text.

Handles ``<think>...</think>`` (reasoning) and
``<tool_call>...</tool_call>`` / ``<tool_call>...<tool_call|>`` (function calling) tags that local models
like Qwen3-Instruct embed in their raw text output.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THINK_START = "<think>"
THINK_END = "</think>"

TOOL_CALL_START = "<tool_call>"
TOOL_CALL_END = "</tool_call>"

# Gemma 4 tool calling format markers.
# Gemma 4 uses <|tool_call> or <|tool_call|> to open and <tool_call|> to close.
GEMMA4_TOOL_CALL_START = "<|tool_call>"
GEMMA4_TOOL_CALL_START_ALT = "<|tool_call|>"
GEMMA4_TOOL_CALL_END = "<tool_call|>"
GEMMA4_QUOTE_TOKEN = '<|"|>'

# Gemma 4 thinking mode emits channel markers, e.g.
# ``<|channel|>thought`` ... ``<channel|>`` before the final answer.
_GEMMA_CHANNEL_OPEN_RE = re.compile(
    r"<\|channel\|?>\s*thought\s*",
    re.IGNORECASE,
)
_GEMMA_CHANNEL_MARKER_RE = re.compile(
    r"<\|?channel\|?>",
    re.IGNORECASE,
)

# Regex to find a complete <think>...</think> block (non-greedy).
_THINK_RE = re.compile(
    r"<think>(.*?)</think>",
    re.DOTALL,
)

# Regex to find complete <tool_call>...</tool_call> blocks (non-greedy).
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)

# Regex for Gemma 4 style tool calls (open tag may be <|tool_call> or <|tool_call|>).
_GEMMA4_TOOL_CALL_RE = re.compile(
    r"<\|tool_call\|?>\s*(.*?)\s*<tool_call\|>",
    re.DOTALL,
)

# Regex for XML-style tool call format:
#   <function=func_name>
#     <parameter=param_name>value</parameter>
#     ...
#   </function>
_XML_FUNC_RE = re.compile(
    r"<function=([^>]+)>(.*?)</function>",
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r"<parameter=([^>]+)>(.*?)</parameter>",
    re.DOTALL,
)

# Regex for lenient XML-style tool call format (no closing tags):
#   <function=func_name>
#     <parameter=param_name>value
#     <parameter=param_name2>value2
_XML_FUNC_LENIENT_RE = re.compile(
    r"<function=([^>]+)>(.*?)(?=<function=|</function>|\Z)",
    re.DOTALL,
)
# Each parameter value runs from after the tag to the next tag or end.
_XML_PARAM_LENIENT_RE = re.compile(
    r"<parameter=([^>]+)>(.*?)"
    r"(?=<parameter=|</parameter>|<function=|</function>|\Z)",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TextWithThinking:
    """Result of extracting ``<think>`` tags from text."""

    # The thinking/reasoning content (between the tags).
    thinking: str = ""
    # The remaining text after removing the ``<think>...</think>`` block.
    remaining_text: str = ""
    # True when ``<think>`` has been opened but ``</think>`` not yet seen
    # (streaming scenario).
    has_open_tag: bool = False


@dataclass
class ParsedToolCall:
    """A single parsed tool call extracted from text."""

    id: str
    name: str
    arguments: dict
    raw_arguments: str


@dataclass
class TextWithToolCalls:
    """Result of parsing text that may contain tool-call tags."""

    # Text content before the first <tool_call> tag.
    text_before: str = ""
    # Text content after the last </tool_call> tag.
    text_after: str = ""
    # Successfully parsed tool calls.
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    # True when an opening <tool_call> has no matching </tool_call> yet
    # (streaming scenario).
    has_open_tag: bool = False
    # Raw text accumulated after the unclosed <tool_call> tag.
    partial_tool_text: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _gemma4_open_tags() -> tuple[str, ...]:
    return (GEMMA4_TOOL_CALL_START_ALT, GEMMA4_TOOL_CALL_START)


def _find_last_gemma4_open(text: str) -> tuple[int, str]:
    best_idx = -1
    best_tag = ""
    for tag in _gemma4_open_tags():
        idx = text.rfind(tag)
        if idx > best_idx:
            best_idx = idx
            best_tag = tag
    return best_idx, best_tag


def _extract_params_lenient(body: str) -> dict:
    """Extract parameters from *body* using lenient regex (no closing tags)."""
    arguments: dict = {}
    for param_match in _XML_PARAM_LENIENT_RE.finditer(body):
        param_name = param_match.group(1).strip()
        param_value = param_match.group(2).strip()
        if param_name:
            arguments[param_name] = param_value
    return arguments


# pylint: disable=too-many-return-statements
def _parse_xml_tool_call(raw_text: str) -> ParsedToolCall | None:
    """Parse an XML-style tool call block.

    Tries the strict format first (all closing tags present)::

        <function=func_name>
          <parameter=param1>value1</parameter>
          <parameter=param2>value2</parameter>
        </function>

    Falls back to a lenient format when closing tags are absent::

        <function=func_name>
          <parameter=param1>value1
          <parameter=param2>value2
    """
    func_match = _XML_FUNC_RE.search(raw_text)
    if func_match:
        name = func_match.group(1).strip()
        if not name:
            return None
        body = func_match.group(2)
        arguments: dict = {}
        for param_match in _XML_PARAM_RE.finditer(body):
            arguments[param_match.group(1).strip()] = param_match.group(
                2,
            ).strip()
        lenient_args = _extract_params_lenient(body)
        if len(lenient_args) > len(arguments):
            arguments = lenient_args
        if not arguments and "<parameter=" in body:
            # Body contains <parameter= tags but neither strict nor lenient
            # parsing could extract them — treat as a parse failure.
            return None
        return ParsedToolCall(
            id=_generate_call_id(),
            name=name,
            arguments=arguments,
            raw_arguments=json.dumps(arguments, ensure_ascii=False),
        )

    # Strict format failed — try lenient format (no closing tags).
    func_match_lenient = _XML_FUNC_LENIENT_RE.search(raw_text)
    if not func_match_lenient:
        return None

    name = func_match_lenient.group(1).strip()
    if not name:
        return None

    body = func_match_lenient.group(2)
    arguments = _extract_params_lenient(body)

    if not arguments:
        logger.debug(
            "Lenient XML parse found function '%s' but no parameters; "
            "discarding.",
            name,
        )
        return None

    logger.debug(
        "Parsed tool call via lenient XML format: name=%s, params=%s",
        name,
        list(arguments.keys()),
    )
    return ParsedToolCall(
        id=_generate_call_id(),
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def _parse_single_tool_call(raw_text: str) -> ParsedToolCall | None:
    """Parse the content between a ``<tool_call>`` / ``</tool_call>`` pair.

    Tries JSON format first::

        {"name": "func_name", "arguments": {"key": "value"}}

    Falls back to strict XML format if JSON parsing fails::

        <function=func_name>
          <parameter=param1>value1</parameter>
        </function>

    Falls back further to lenient XML format (no closing tags) if needed::

        <function=func_name>
          <parameter=param1>value1
          <parameter=param2>value2
    """
    stripped = raw_text.strip()

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        data = None

    if data is not None:
        name = data.get("name", "")
        if not name:
            logger.warning(
                "Tool call missing 'name' field: %s",
                stripped[:200],
            )
            return None

        arguments = data.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}

        return ParsedToolCall(
            id=_generate_call_id(),
            name=name,
            arguments=arguments,
            raw_arguments=json.dumps(arguments, ensure_ascii=False),
        )

    # JSON failed — try XML format.
    result = _parse_xml_tool_call(stripped)
    if result is None:
        logger.warning("Failed to parse tool call: %s", stripped[:200])
    return result


def _extract_after_segments(matches, text):
    """Compute text_before, text_after, and streaming state.

    Shared between standard and Gemma 4 parsing paths.
    *matches* is a list of ``re.Match`` objects sorted by start position.
    """
    if not matches:
        return text, "", False, ""

    text_before = text[: matches[0].start()].rstrip()
    remaining = text[matches[-1].end() :]

    g4_idx, g4_tag = _find_last_gemma4_open(remaining)
    std_idx = remaining.rfind(TOOL_CALL_START)
    if g4_idx != -1 and g4_idx >= std_idx:
        text_after = remaining[:g4_idx].strip()
        has_open = True
        partial = remaining[g4_idx + len(g4_tag) :]
    elif std_idx != -1:
        text_after = remaining[:std_idx].strip()
        has_open = True
        partial = remaining[std_idx + len(TOOL_CALL_START) :]
    else:
        text_after = remaining.strip()
        has_open = False
        partial = ""

    return text_before, text_after, has_open, partial


def _normalize_gemma_quotes(text: str) -> str:
    """Replace Gemma 4 quote tokens with standard double-quote characters."""
    return text.replace(GEMMA4_QUOTE_TOKEN, '"')


def _extract_kv_from_gemma_body(body: str) -> dict:
    """Extract key-value pairs from a Gemma 4 tool-call argument body.

    Handles both quoted values (with standard ``"`` or Gemma quote
    tokens) and unquoted values (numbers, booleans).
    """
    normalized = _normalize_gemma_quotes(body)

    # Attempt 1: wrap in braces and parse as JSON.
    try:
        result = json.loads("{" + normalized + "}")
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Attempt 2: single-quoted variant.
    try:
        result = json.loads("{" + normalized.replace("'", '"') + "}")
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Fallback: regex extraction.
    arguments: dict = {}
    quoted_re = re.compile(
        r'(\w+)\s*:\s*(?:' + re.escape(GEMMA4_QUOTE_TOKEN) + r'|")'
        r'(.*?)'
        r'(?:' + re.escape(GEMMA4_QUOTE_TOKEN) + r'|")',
        re.DOTALL,
    )
    for m in quoted_re.finditer(body):
        arguments[m.group(1).strip()] = m.group(2)

    unquoted_re = re.compile(
        r'(\w+)\s*:\s*([^,}"\'<]+)',
    )
    for m in unquoted_re.finditer(normalized):
        key = m.group(1).strip()
        val = m.group(2).strip()
        if key and key not in arguments:
            arguments[key] = val

    return arguments


def _parse_gemma_tool_call(raw_text: str) -> ParsedToolCall | None:
    """Parse a Gemma 4 tool-call body.

    Expected format::

        call:function_name{key1:"value1", key2:"value2"}

    String values may be delimited with standard double-quotes or with
    Gemma 4's special quote tokens.  Numeric / boolean values may also
    appear unquoted.
    """
    stripped = raw_text.strip()

    # JSON fallback (for models that emit JSON inside Gemma 4 tags).
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and "name" in data:
            arguments = data.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
            return ParsedToolCall(
                id=_generate_call_id(),
                name=data["name"],
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
    except (json.JSONDecodeError, TypeError):
        pass

    if not stripped.startswith("call:"):
        return None

    body = stripped[len("call:"):]

    brace_idx = body.find("{")
    if brace_idx == -1:
        name = body.strip()
        if not name:
            return None
        return ParsedToolCall(
            id=_generate_call_id(),
            name=name,
            arguments={},
            raw_arguments="{}",
        )

    name = body[:brace_idx].strip()
    if not name:
        return None

    args_body = body[brace_idx + 1:]
    if args_body.endswith("}"):
        args_body = args_body[:-1]
    args_body = args_body.strip()

    if not args_body:
        arguments: dict = {}
    else:
        arguments = _extract_kv_from_gemma_body(args_body)

    return ParsedToolCall(
        id=_generate_call_id(),
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def _parse_gemma4_tool_calls(text: str) -> list[ParsedToolCall]:
    """Parse Gemma 4 style tool calls from *text*.

    Gemma 4 wraps tool call bodies between special start/end tokens.
    The body uses the ``call:func_name{key:"value"}`` format, with a
    JSON fallback for models that emit standard JSON tool calls.
    """
    results: list[ParsedToolCall] = []
    for m in _GEMMA4_TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        if not raw:
            continue
        # Native call: format or single JSON object.
        pc = _parse_gemma_tool_call(raw)
        if pc is not None:
            results.append(pc)
            continue
        # Fallback: JSON array of tool call objects.
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or "name" not in item:
                    continue
                arguments = item.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                results.append(ParsedToolCall(
                    id=_generate_call_id(),
                    name=item["name"],
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                ))
    return results


def _find_all_tool_call_matches(text: str) -> list:
    """Find all complete tool call blocks in *text*.

    Searches for both standard <tool_call>/</tool_call> blocks
    and Gemma 4 <tool_call>/<tool_call|> blocks.
    Returns a list of ``re.Match`` objects sorted by start position.
    """
    std_matches = list(_TOOL_CALL_RE.finditer(text))
    g4_matches = list(_GEMMA4_TOOL_CALL_RE.finditer(text))
    all_matches = std_matches + g4_matches
    all_matches.sort(key=lambda m: m.start())
    return all_matches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def strip_gemma_channel_markup(text: str) -> str:
    """Remove Gemma 4 ``channel`` / ``thought`` delimiter tokens from *text*.

    Cloud Gemma may prefix thinking or answer segments with tokens such as
    ``<|channel|>thought`` and ``<channel|>`` instead of structured fields.
    """
    if not text:
        return text
    cleaned = _GEMMA_CHANNEL_OPEN_RE.sub("", text)
    cleaned = _GEMMA_CHANNEL_MARKER_RE.sub("", cleaned)
    return cleaned.strip()


def text_contains_think_tag(text: str) -> bool:
    """Fast substring check for a ``<think>`` tag."""
    return THINK_START in text


def extract_thinking_from_text(text: str) -> TextWithThinking:
    """Extract ``<think>...</think>`` content from *text*.

    Returns a :class:`TextWithThinking` with:

    * ``thinking``       – the reasoning content (empty if none found)
    * ``remaining_text`` – everything outside the think tags
    * ``has_open_tag``   – ``True`` if ``<think>`` opened but not closed yet
    """
    match = _THINK_RE.search(text)
    if match:
        thinking = match.group(1).strip()
        remaining = (text[: match.start()] + text[match.end() :]).strip()
        return TextWithThinking(
            thinking=thinking,
            remaining_text=remaining,
        )

    # No complete block — check for an unclosed <think>.
    open_idx = text.find(THINK_START)
    if open_idx != -1:
        remaining = text[:open_idx].strip()
        partial = text[open_idx + len(THINK_START) :]
        return TextWithThinking(
            thinking=partial.strip(),
            remaining_text=remaining,
            has_open_tag=True,
        )

    return TextWithThinking(remaining_text=text)


def text_contains_tool_call_tag(text: str) -> bool:
    """Fast substring check for a ``<tool_call>`` tag.

    Also checks for the Gemma 4 variant (same opening marker,
    closed by ``<tool_call|>`` instead of ``</tool_call>``).
    """
    if TOOL_CALL_START in text:
        return True
    return any(tag in text for tag in _gemma4_open_tags())


def parse_tool_calls_from_text(text: str) -> TextWithToolCalls:
    """Extract all tool call blocks from *text*.

    Handles both the standard ``<tool_call>...</tool_call>`` format
    and the Gemma 4 ``<tool_call>...<tool_call|>`` format.

    Returns a :class:`TextWithToolCalls` with:

    * ``text_before`` – all text before the first tool call tag
    * ``text_after``  – all text after the last closing tag
    * ``tool_calls``  – successfully parsed tool calls
    * ``has_open_tag`` – whether there is an unclosed opening tag
        (streaming)
    * ``partial_tool_text`` – content after the unclosed tag
    """
    # Find all complete tool call blocks (both formats).
    matches = _find_all_tool_call_matches(text)

    if not matches:
        # No complete blocks — check for an unclosed opening tag.
        g4_idx, g4_tag = _find_last_gemma4_open(text)
        std_idx = text.rfind(TOOL_CALL_START)
        if g4_idx == -1 and std_idx == -1:
            return TextWithToolCalls(text_before=text)
        if g4_idx >= std_idx:
            open_idx, tag = g4_idx, g4_tag
        else:
            open_idx, tag = std_idx, TOOL_CALL_START
        return TextWithToolCalls(
            text_before=text[:open_idx].rstrip(),
            has_open_tag=True,
            partial_tool_text=text[open_idx + len(tag) :],
        )

    # Extract text segments and streaming state.
    text_before, text_after, has_open, partial = _extract_after_segments(
        matches, text,
    )

    # Parse each complete block.
    tool_calls: list[ParsedToolCall] = []
    for m in matches:
        raw = m.group(1)
        if GEMMA4_TOOL_CALL_END in m.group(0):
            # Gemma 4 format: closed by <tool_call|>
            tool_calls.extend(_parse_gemma4_tool_calls(m.group(0)))
        else:
            # Standard format: closed by </tool_call>
            parsed = _parse_single_tool_call(raw)
            if parsed is not None:
                tool_calls.append(parsed)

    return TextWithToolCalls(
        text_before=text_before,
        text_after=text_after,
        tool_calls=tool_calls,
        has_open_tag=has_open,
        partial_tool_text=partial,
    )
