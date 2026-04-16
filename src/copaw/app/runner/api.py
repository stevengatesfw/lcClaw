# -*- coding: utf-8 -*-
"""Chat management API."""
from __future__ import annotations

import os
from typing import Callable, Optional

from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from agentscope.memory import InMemoryMemory

from ...app.auth import get_current_user_id_required
from .session import SafeJSONSession
from .manager import ChatManager
from .models import (
    ChatSpec,
    ChatUpdate,
    ChatHistory,
)
from .utils import agentscope_msg_to_message


router = APIRouter(prefix="/chats", tags=["chats"])

_ISOLATION_ENABLED = bool(os.environ.get("LAZY_PLATFORM_KEY", "").strip())


def _get_chat_manager_factory(
    request: Request,
) -> Callable[[str], ChatManager]:
    """Get chat manager factory from app state (LCAgent user isolation)."""
    factory = getattr(request.app.state, "chat_manager_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="Chat manager not initialized",
        )
    return factory


async def get_workspace(request: Request):
    """Get the workspace for the active agent."""
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def get_chat_manager(
    request: Request,
    uid: str = Depends(get_current_user_id_required),
) -> ChatManager:
    """Per-user ChatManager if isolation; else active agent workspace."""
    if _ISOLATION_ENABLED:
        factory = _get_chat_manager_factory(request)
        return factory(uid or "")
    workspace = await get_workspace(request)
    cm = workspace.chat_manager
    if cm is None:
        raise HTTPException(
            status_code=503,
            detail="Chat manager not initialized",
        )
    return cm


async def get_session(request: Request) -> SafeJSONSession:
    """Session store for the active agent (multi-agent path)."""
    workspace = await get_workspace(request)
    return workspace.runner.session


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    channel: Optional[str] = Query(None, description="Filter by channel"),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
    uid: str = Depends(get_current_user_id_required),
):
    """List chats; per-user filter when LCAgent JWT isolation is on."""
    user_filter = uid if _ISOLATION_ENABLED else None
    chats = await mgr.list_chats(user_id=user_filter, channel=channel)
    if _ISOLATION_ENABLED:
        return chats
    tracker = workspace.task_tracker
    result = []
    for spec in chats:
        status = await tracker.get_status(spec.id)
        result.append(spec.model_copy(update={"status": status}))
    return result


@router.post("", response_model=ChatSpec)
async def create_chat(
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager),
    current_user_id: str = Depends(get_current_user_id_required),
):
    """Create a new chat."""
    chat_id = str(uuid4())
    user_id = (
        current_user_id
        if _ISOLATION_ENABLED
        else (request.user_id or "anonymous")
    )
    spec = ChatSpec(
        id=chat_id,
        name=request.name,
        session_id=request.session_id,
        user_id=user_id,
        channel=request.channel,
        meta=request.meta,
    )
    return await mgr.create_chat(spec)


@router.post("/batch-delete", response_model=dict)
async def batch_delete_chats(
    chat_ids: list[str],
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Delete chats by chat IDs."""
    deleted = await mgr.delete_chats(chat_ids=chat_ids)
    return {"deleted": deleted}


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get detailed information about a specific chat by UUID."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    state = await session.get_session_state_dict(
        chat_spec.session_id,
        chat_spec.user_id,
    )
    status = await workspace.task_tracker.get_status(chat_id)
    if not state:
        return ChatHistory(messages=[], status=status)
    memory_state = state.get("agent", {}).get("memory", {})
    memory = InMemoryMemory()
    memory.load_state_dict(memory_state, strict=False)

    memories = await memory.get_memory(prepend_summary=False)
    messages = agentscope_msg_to_message(memories)
    return ChatHistory(messages=messages, status=status)


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    spec: ChatUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Update an existing chat."""
    if spec.id != chat_id:
        raise HTTPException(
            status_code=400,
            detail="chat_id mismatch",
        )

    updated = await mgr.patch_chat(chat_id, spec)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return updated


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Delete a chat by UUID."""
    deleted = await mgr.delete_chats(chat_ids=[chat_id])
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return {"deleted": True}
