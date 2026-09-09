# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

import asyncio
import json
from datetime import date
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from pydantic import BaseModel

from .manager import get_token_usage_manager

_TOKEN_ESTIMATE_DIVISOR = 3.75


def _estimate_tokens(text: str) -> int:
    """Use the same lightweight fallback ratio as CopawTokenCounter."""
    if not text:
        return 0
    return max(int(len(text.encode("utf-8")) / _TOKEN_ESTIMATE_DIVISOR + 0.5), 1)


def _response_parts(response: ChatResponse) -> dict[str, str]:
    """Extract generated content by stable block type and position."""
    parts: dict[str, str] = {}
    for index, block in enumerate(response.content or []):
        block_type = (
            block.get("type", "content")
            if isinstance(block, dict)
            else "content"
        )
        key = f"{block_type}:{index}"
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts[key] = text
                continue
            parts[key] = json.dumps(block, ensure_ascii=False, default=str)
        else:
            parts[key] = str(block)
    return parts


def _merge_stream_text(current: str, chunk: str) -> str:
    """Support providers that stream either deltas or cumulative content."""
    if not chunk:
        return current
    if chunk.startswith(current):
        return chunk
    if current.startswith(chunk):
        return current
    max_overlap = min(len(current), len(chunk))
    for overlap in range(max_overlap, 0, -1):
        if current.endswith(chunk[:overlap]):
            return current + chunk[overlap:]
    return current + chunk


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    def __init__(self, provider_id: str, model: ChatModelBase) -> None:
        super().__init__(
            model_name=getattr(model, "model_name", "unknown"),
            stream=getattr(model, "stream", True),
        )
        self._model = model
        self._provider_id = provider_id

    async def _record_usage(self, usage: ChatUsage | None) -> None:
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        if pt > 0 or ct > 0:
            await get_token_usage_manager().record(
                provider_id=self._provider_id,
                model_name=self.model_name,
                prompt_tokens=pt,
                completion_tokens=ct,
                at_date=date.today(),
            )

    async def _record_cancelled_estimate(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        generated_text: str,
    ) -> None:
        """Record partial usage when cancellation removes final provider usage."""
        prompt_text = json.dumps(
            {"messages": messages, "tools": tools or []},
            ensure_ascii=False,
            default=str,
        )
        prompt_tokens = _estimate_tokens(prompt_text)
        completion_tokens = _estimate_tokens(generated_text)
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        await get_token_usage_manager().record(
            provider_id=self._provider_id,
            model_name=self.model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            at_date=date.today(),
        )

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result, messages, tools)
        await self._record_usage(getattr(result, "usage", None))
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        messages: list[dict],
        tools: list[dict] | None,
    ) -> AsyncGenerator[ChatResponse, None]:
        async for chunk in self._wrap_full_billing_stream(
            stream,
            messages,
            tools,
        ):
            yield chunk

    async def _wrap_full_billing_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        messages: list[dict],
        tools: list[dict] | None,
    ) -> AsyncGenerator[ChatResponse, None]:
        """Suppress output on stop while draining a fully billable request.

        All providers use one platform policy: an accepted model request is
        billed for its complete generation after the user stops the UI stream.
        A separate task suppresses output immediately while billing waits for
        the exact final usage packet.
        """
        queue: asyncio.Queue[ChatResponse | object] = asyncio.Queue()
        done = object()
        consumer_active = True
        producer_error: BaseException | None = None

        async def consume_provider_stream() -> None:
            nonlocal producer_error
            last_usage: ChatUsage | None = None
            generated_parts: dict[str, str] = {}
            try:
                async for chunk in stream:
                    if getattr(chunk, "usage", None) is not None:
                        last_usage = chunk.usage
                    for key, text in _response_parts(chunk).items():
                        generated_parts[key] = _merge_stream_text(
                            generated_parts.get(key, ""),
                            text,
                        )
                    if consumer_active:
                        queue.put_nowait(chunk)
            except BaseException as exc:
                producer_error = exc
            finally:
                if last_usage is not None:
                    await self._record_usage(last_usage)
                elif generated_parts:
                    await self._record_cancelled_estimate(
                        messages,
                        tools,
                        "".join(generated_parts.values()),
                    )
                if consumer_active:
                    queue.put_nowait(done)

        producer = asyncio.create_task(consume_provider_stream())
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            consumer_active = False
            await asyncio.shield(producer)
            raise
        else:
            await producer
            if producer_error is not None:
                raise producer_error
