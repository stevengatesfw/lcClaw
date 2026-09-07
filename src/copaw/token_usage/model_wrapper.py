# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses.

同时承担 P0 缓存可观测性（CacheDiagnostics，链路 A）：每次模型调用输出单行
``cache_diag`` 日志（provider / resolved model / endpoint / input / cached /
命中率 / 与同 session 上一请求的共享前缀与第一个差异块）。诊断只读：不修改
请求内容、不修改历史语义，内部吞掉全部异常，``LCAGENT_CACHE_DIAG=0`` 可关闭。
"""

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
            return self._wrap_stream(result, diag_state)
        self._diag_finish(diag_state, getattr(result, "usage", None))
        await self._record_usage(getattr(result, "usage", None))
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        diag_state: dict | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        last_usage: ChatUsage | None = None
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                last_usage = chunk.usage
            yield chunk
        self._diag_finish(diag_state, last_usage)
        await self._record_usage(last_usage)
