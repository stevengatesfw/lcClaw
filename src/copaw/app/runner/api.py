# -*- coding: utf-8 -*-
"""Chat management API."""
from __future__ import annotations

import os
from typing import Callable, Optional

from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from agentscope.memory import InMemoryMemory

from ...app.auth import get_current_user_id_required
from .edit_resend import delete_user_turns, normalize_user_turn_indices
from .session import SafeJSONSession
from .manager import ChatManager
from .models import (
    ChatSpec,
    ChatUpdate,
    ChatHistory,
    ChatPruneRequest,
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


@router.post("/prune", response_model=dict)
async def prune_chat_turns(
    request: ChatPruneRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
):
    """Drop whole user turns from a chat, keeping its session id.

    Backs the console "delete turn" action. Rewriting the persisted memory in
    place is what lets the chat stay one entry in the history list; abandoning
    the session for a fresh id used to leave the deleted turn behind and add a
    new conversation.
    """
    try:
        turn_indices = normalize_user_turn_indices(request.user_turn_indices)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not turn_indices:
        raise HTTPException(
            status_code=400,
            detail="user_turn_indices must not be empty",
        )

    chat_id = await mgr.get_chat_id_by_session(
        request.session_id,
        request.channel,
    )
    chat_spec = await mgr.get_chat(chat_id) if chat_id else None
    if chat_spec is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found for session: {request.session_id}",
        )

    state = await session.get_session_state_dict(
        request.session_id,
        chat_spec.user_id,
    )
    memory_state = state.get("agent", {}).get("memory", {})
    if not memory_state:
        # Nothing persisted to prune (new chat, or state dropped). The client
        # already removed the turn locally, so this is a success, not an error.
        return {"removed": 0}

    memory = InMemoryMemory()
    memory.load_state_dict(memory_state, strict=False)
    removed = await delete_user_turns(memory, turn_indices)
    if not removed:
        return {"removed": 0}

    pruned_state = memory.state_dict()
    await session.update_session_state(
        session_id=request.session_id,
        key="agent.memory",
        value=pruned_state,
        user_id=chat_spec.user_id,
    )

    # The platform reads /files/chat/{session_id} from the TiDB copy, so it has
    # to be refreshed too or the pruned turns keep resurfacing from there.
    from .repo.db_repo import clear_messages_in_db, sync_messages_to_db
    if any(
        isinstance(entry, (list, tuple))
        and entry
        and getattr(entry[0], "role", None) != "system"
        for entry in memory.content
    ):
        await sync_messages_to_db(
            request.session_id,
            chat_spec.user_id,
            {"agent": {"memory": pruned_state}},
        )
    else:
        # Pruned the chat empty: sync would no-op on an empty memory and leave
        # the old rows behind, so clear them explicitly.
        await clear_messages_in_db(request.session_id)

    return {"removed": removed}


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
