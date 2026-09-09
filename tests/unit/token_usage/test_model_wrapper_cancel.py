import asyncio
from unittest.mock import AsyncMock

import pytest
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from copaw.token_usage.model_wrapper import (
    TokenRecordingModelWrapper,
    _estimate_tokens,
    _merge_stream_text,
)


def test_estimate_tokens_counts_utf8_content():
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("测试内容") > 0


def test_merge_stream_text_supports_delta_chunks():
    assert _merge_stream_text("第一段", "第二段") == "第一段第二段"


def test_merge_stream_text_supports_cumulative_chunks():
    assert _merge_stream_text("第一段", "第一段第二段") == "第一段第二段"


def test_merge_stream_text_does_not_duplicate_overlapping_chunks():
    assert _merge_stream_text("第一段第二", "第二段第三段") == "第一段第二段第三段"


@pytest.mark.parametrize("provider", ["ppio", "qwen", "openai", "custom"])
@pytest.mark.asyncio
async def test_full_billing_stop_drains_final_usage_before_cleanup(provider):
    release_final = asyncio.Event()
    first_chunk_seen = asyncio.Event()
    exact_usage = ChatUsage(input_tokens=10, output_tokens=20, time=1.0)

    async def provider_stream():
        yield ChatResponse(content=[{"type": "text", "text": "部分"}])
        await release_final.wait()
        yield ChatResponse(
            content=[{"type": "text", "text": "部分完整"}],
            usage=exact_usage,
        )

    wrapper = object.__new__(TokenRecordingModelWrapper)
    wrapper._provider_id = provider
    wrapper._record_usage = AsyncMock()
    wrapper._record_cancelled_estimate = AsyncMock()

    async def consume():
        async for _chunk in wrapper._wrap_stream(
            provider_stream(),
            [],
            None,
        ):
            first_chunk_seen.set()

    task = asyncio.create_task(consume())
    await first_chunk_seen.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_final.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    wrapper._record_usage.assert_awaited_once_with(exact_usage)
    wrapper._record_cancelled_estimate.assert_not_awaited()
