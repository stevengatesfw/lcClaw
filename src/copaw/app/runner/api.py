# -*- coding: utf-8 -*-
"""Chat management API."""
from __future__ import annotations
import json
import os

from typing import Callable, Optional
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from agentscope.session import JSONSession
from agentscope.memory import InMemoryMemory

from ...app.auth import get_current_user_id_required
from .manager import ChatManager
from .models import (
    ChatSpec,
    ChatHistory,
)
from .utils import agentscope_msg_to_message


router = APIRouter(prefix="/chats", tags=["chats"])

_ISOLATION_ENABLED = bool(os.environ.get("LAZY_PLATFORM_KEY", "").strip())


def _get_chat_manager_factory(request: Request) -> Callable[[str], ChatManager]:
    """Get chat manager factory from app state."""
    factory = getattr(request.app.state, "chat_manager_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="Chat manager not initialized",
        )
    return factory


def get_chat_manager_for_request(
    request: Request,
    current_user_id: str = Depends(get_current_user_id_required),
) -> ChatManager:
    """Get ChatManager for current user (when isolation enabled)."""
    factory = _get_chat_manager_factory(request)
    return factory(current_user_id)


def get_session(request: Request) -> JSONSession:
    """Get the session from app state.

    Args:
        request: FastAPI request object

    Returns:
        JSONSession instance

    Raises:
        HTTPException: If session is not initialized
    """
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="Session not initialized",
        )
    return runner.session


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    channel: Optional[str] = Query(None, description="Filter by channel"),
    mgr: ChatManager = Depends(get_chat_manager_for_request),
    current_user_id: str = Depends(get_current_user_id_required),
):
    """List all chats for current user (user_id from JWT when isolation enabled).

    Args:
        channel: Optional channel name to filter chats
        mgr: Chat manager dependency (per-user when isolation enabled)
        current_user_id: From JWT; empty when isolation disabled
    """
    user_filter = current_user_id if _ISOLATION_ENABLED else None
    return await mgr.list_chats(user_id=user_filter, channel=channel)


@router.post("", response_model=ChatSpec)
async def create_chat(
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager_for_request),
    current_user_id: str = Depends(get_current_user_id_required),
):
    """Create a new chat.

    Server generates chat_id (UUID) automatically.
    When isolation enabled, user_id is forced from JWT.

    Args:
        request: Chat creation request
        mgr: Chat manager dependency
        current_user_id: From JWT; used as user_id when isolation enabled
    """
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
    mgr: ChatManager = Depends(get_chat_manager_for_request),
):
    """Delete chats by chat IDs.

    Args:
        chat_ids: List of chat IDs
        mgr: Chat manager dependency
    Returns:
        True if deleted, False if failed

    """
    deleted = await mgr.delete_chats(chat_ids=chat_ids)
    return {"deleted": deleted}


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager_for_request),
    session: JSONSession = Depends(get_session),
):
    """Get detailed information about a specific chat by UUID.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency
        session: JSONSession  dependency

    Returns:
        ChatHistory with messages

    Raises:
        HTTPException: If chat not found (404)
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    # pylint: disable=protected-access
    session_path = session._get_save_path(
        chat_spec.session_id,
        chat_spec.user_id,
    )

    try:
        with open(session_path, "r", encoding="utf-8") as file:
            state = json.load(file)
    except Exception:
        return ChatHistory(messages=[])
    memories = state.get("agent", {}).get("memory", [])
    memory = InMemoryMemory()
    memory.load_state_dict(memories)

    memories = await memory.get_memory()
    messages = agentscope_msg_to_message(memories)
    return ChatHistory(messages=messages)


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    spec: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager_for_request),
):
    """Update an existing chat.

    Args:
        chat_id: Chat UUID
        spec: Updated chat specification
        mgr: Chat manager dependency

    Returns:
        Updated chat spec

    Raises:
        HTTPException: If chat_id mismatch (400) or not found (404)
    """
    if spec.id != chat_id:
        raise HTTPException(
            status_code=400,
            detail="chat_id mismatch",
        )

    # Check if exists
    existing = await mgr.get_chat(chat_id)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    updated = await mgr.update_chat(spec)
    return updated


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager_for_request),
):
    """Delete a chat by UUID.

    Note: This only deletes the chat spec (UUID mapping).
    JSONSession state is NOT deleted.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency

    Returns:
        True if deleted, False if failed

    Raises:
        HTTPException: If chat not found (404)
    """
    deleted = await mgr.delete_chats(chat_ids=[chat_id])
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return {"deleted": True}
