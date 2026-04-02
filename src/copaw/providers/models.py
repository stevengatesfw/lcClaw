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
    chat_model_name: Optional[str] = Field(
        default=None,
        description="When set, use this chat model class name (e.g. OpenAIChatModel).",
    )
