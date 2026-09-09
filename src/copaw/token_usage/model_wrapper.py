# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses.

同时承担 P0 缓存可观测性（CacheDiagnostics，链路 A）：每次模型调用输出单行
``cache_diag`` 日志（provider / resolved model / endpoint / input / cached /
命中率 / 与同 session 上一请求的共享前缀与第一个差异块）。诊断只读：不修改
请求内容、不修改历史语义，内部吞掉全部异常，``LCAGENT_CACHE_DIAG=0`` 可关闭。
"""

import asyncio
import json
import logging
from datetime import date
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from pydantic import BaseModel

from .cache_diagnostics import (
    begin_request_diagnostics,
    finish_request_diagnostics,
    normalize_cache_usage,
)
from .manager import get_token_usage_manager

logger = logging.getLogger(__name__)

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

    # ---- CacheDiagnostics（链路 A，只读打点） ------------------------------

    def _diag_session(self) -> str:
        try:
            from ..context import get_context_session_id

            sid = get_context_session_id()
            if sid:
                return str(sid)
        except Exception:
            pass
        # 无请求上下文（后台任务/标题生成等）时退化为按 provider+model 对比。
        return f"copaw:{self._provider_id}:{self.model_name}"

    def _diag_endpoint(self) -> str | None:
        """resolved base_url：agentscope OpenAI 系模型把客户端挂在 ``client``。"""
        client = getattr(self._model, "client", None)
        base = getattr(client, "base_url", None)
        if base is None:
            return None
        return str(base)

    def _diag_begin(self, messages: list[dict], tools: list[dict] | None):
        try:
            return begin_request_diagnostics(
                session=self._diag_session(),
                messages=messages,
                tools=tools,
            )
        except Exception:
            return None

    def _diag_finish(self, state, usage: ChatUsage | None) -> None:
        if state is None:
            return
        try:
            # agentscope 1.0.18 把 provider 原始 usage 对象挂在 ChatUsage.metadata
            #（含 prompt_tokens_details.cached_tokens / prompt_cache_hit_tokens 等），
            # 缺失时退化为 ChatUsage 本身（只有 input/output_tokens）。
            raw = getattr(usage, "metadata", None)
            finish_request_diagnostics(
                state,
                chain="A",
                provider=self._provider_id,
                model=self.model_name,
                endpoint=self._diag_endpoint(),
                raw_usage=raw if raw is not None else usage,
                logger=logger,
            )
        except Exception:
            pass

    async def _record_usage(self, usage: ChatUsage | None) -> None:
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        cached = 0
        try:
            raw = getattr(usage, "metadata", None)
            norm = normalize_cache_usage(raw if raw is not None else usage)
            cached = int(norm.get("cached_tokens") or 0)
        except Exception:
            cached = 0
        if pt > 0 or ct > 0:
            await get_token_usage_manager().record(
                provider_id=self._provider_id,
                model_name=self.model_name,
                prompt_tokens=pt,
                completion_tokens=ct,
                cached_tokens=cached,
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
        diag_state = self._diag_begin(messages, tools)
        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(
                result,
                messages,
                tools,
                diag_state,
            )

        usage = getattr(result, "usage", None)
        self._diag_finish(diag_state, usage)
        await self._record_usage(usage)
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        messages: list[dict],
        tools: list[dict] | None,
        diag_state: dict | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        async for chunk in self._wrap_full_billing_stream(
            stream,
            messages,
            tools,
            diag_state,
        ):
            yield chunk

    async def _wrap_full_billing_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        messages: list[dict],
        tools: list[dict] | None,
        diag_state: dict | None = None,
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
                self._diag_finish(diag_state, last_usage)
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