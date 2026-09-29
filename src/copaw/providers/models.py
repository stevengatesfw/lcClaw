# -*- coding: utf-8 -*-
"""Pydantic data models for providers and models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ModelSlotConfig(BaseModel):
    provider_id: str = Field(default="")
    model: str = Field(default="")


class ResolvedModelConfig(BaseModel):
    """LCAgent home page resolved LLM (injected via agent/process ``meta``)."""

    model: str = Field(default="")
    base_url: str = Field(default="")
    api_key: str = Field(default="")
    is_local: bool = Field(default=False)
    enable_thinking: bool = Field(
        default=False,
        description="Whether the upstream third-party model may emit reasoning.",
    )
    chat_model_name: Optional[str] = Field(
        default=None,
        description=(
            "When set, use this chat model class name (e.g. OpenAIChatModel)."
        ),
    )
    max_output_tokens: Optional[int] = Field(
        default=None,
        description=(
            "Per-model maximum output (completion) tokens. When None, CoPaw "
            "applies AgentsRunningConfig.max_output_tokens. Setting this "
            "prevents long answers (e.g. generated documents) from being cut "
            "off by the provider's small server-side default."
        ),
    )
