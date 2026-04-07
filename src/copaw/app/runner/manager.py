# -*- coding: utf-8 -*-
"""Chat manager for managing chat specifications."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

from ...config.utils import DEFAULT_USER_ID, copaw_storage_isolation_enabled
from ...context import get_context_user_id
from .models import ChatSpec
from .repo import BaseChatRepository
from ..channels.schema import DEFAULT_CHANNEL

logger = logging.getLogger(__name__)


class ChatManager:
    """Manages chat specifications in repository.

    Only handles ChatSpec CRUD operations.
    Does NOT manage Redis session state - that's handled by runner's session.

    Similar to CronManager's role in crons module.
    """

    def __init__(
        self,
        *,
        repo: BaseChatRepository,
    ):
        """Initialize chat manager.

        Args:
            repo: Chat spec repository for persistence
        """
        self._repo = repo
        self._lock = asyncio.Lock()
        logger.info(
            f"ChatManager created with repo path: {repo.path}",
        )

    # ----- Read Operations -----

    async def list_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> list[ChatSpec]:
        """List chat specs with optional filters.

        Args:
            user_id: Optional user ID filter
            channel: Optional channel filter

        Returns:
            List of chat specifications
        """
        async with self._lock:
            logger.debug(
                f"list_chats: repo path={self._repo.path}, "
                f"filters: user_id={user_id}, channel={channel}",
            )
            return await self._repo.filter_chats(
                user_id=user_id,
                channel=channel,
            )

    async def get_chat(self, chat_id: str) -> Optional[ChatSpec]:
        """Get chat spec by chat_id (UUID).

        Args:
            chat_id: Chat UUID

        Returns:
            Chat spec or None if not found
        """
        async with self._lock:
            return await self._repo.get_chat(chat_id)

    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
        name: str = "New Chat",
    ) -> ChatSpec:
        """Get existing chat or create new one.

        Useful for auto-registration when chats come from channels.

        Args:
            session_id: Session identifier (channel:user_id)
            user_id: User identifier
            channel: Channel name
            name: Chat name

        Returns:
            Chat specification (existing or newly created)
        """
        async with self._lock:
            # Try to find existing by session_id
            logger.debug(
                f"get_or_create_chat: Searching for existing chat: "
                f"session_id={session_id}, user_id={user_id}, "
                f"channel={channel}",
            )
            existing = await self._repo.get_chat_by_id(
                session_id,
                user_id,
                channel,
            )
            if existing:
                logger.debug(
                    f"get_or_create_chat: Found existing chat: {existing.id}",
                )
                return existing

            # Create new
            logger.debug(
                f"get_or_create_chat: Creating new chat for "
                f"session_id={session_id}",
            )
            spec = ChatSpec(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                name=name,
            )
            logger.debug(f"get_or_create_chat: created spec={spec.id}")
            # Call internal create without lock (already locked)
            await self._repo.upsert_chat(spec)
            logger.debug(
                f"Auto-registered new chat: {spec.id} -> {session_id}",
            )
            return spec

    async def create_chat(self, spec: ChatSpec) -> ChatSpec:
        """Create a new chat.

        Args:
            spec: Chat specification (chat_id will be generated if not set)

        Returns:
            Chat spec
        """
        async with self._lock:
            await self._repo.upsert_chat(spec)
            return spec

    async def update_chat(self, spec: ChatSpec) -> ChatSpec:
        """Update an existing chat spec.

        Args:
            spec: Updated chat specification

        Returns:
            Updated chat spec
        """
        async with self._lock:
            spec.updated_at = datetime.now(timezone.utc)
            await self._repo.upsert_chat(spec)
            return spec

    async def delete_chats(self, chat_ids: list[str]) -> bool:
        """Delete a chat spec.

        Note: This only deletes the spec. Redis session state is NOT deleted.

        Args:
            chat_ids: List of chat IDs

        Returns:
            True if deleted, False if not found
        """
        async with self._lock:
            deleted = await self._repo.delete_chats(chat_ids)

            if deleted:
                logger.debug(f"Deleted chats: {chat_ids}")

            return deleted

    async def count_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> int:
        """Count chats matching filters.

        Args:
            user_id: Optional user ID filter
            channel: Optional channel filter

        Returns:
            Number of matching chats
        """
        async with self._lock:
            chats = await self._repo.filter_chats(
                user_id=user_id,
                channel=channel,
            )
            return len(chats)

    async def get_chat_id_by_session(
        self,
        session_id: str,
        channel: str,
    ) -> str | None:
        """Get chat_id by session_id and channel.

        Args:
            session_id: Normalized session ID (e.g. "console:user1")
            channel: Channel name

        Returns:
            chat_id (UUID) of most recent chat if found, None otherwise

        Note:
            Returns most recently updated chat if multiple matches exist.
            O(N) scan of active chats. Future optimization: add index.
        """
        async with self._lock:
            chats = await self._repo.filter_chats(channel=channel)
            matching_chats = [
                chat for chat in chats if chat.session_id == session_id
            ]

            if not matching_chats:
                logger.debug(
                    f"No chat found for session={session_id[:30]} "
                    f"channel={channel}",
                )
                return None

            most_recent = max(matching_chats, key=lambda c: c.updated_at)
            logger.debug(
                f"Found chat_id={most_recent.id} "
                f"for session={session_id[:30]} "
                f"(from {len(matching_chats)} matches)",
            )
            return most_recent.id


class TenantRoutingChatManager:
    """Route chat specs to ``users/<uid>/chats.json`` when LCAgent isolation is on.

    Workspace services and the runner must use the same persistence as
    ``GET /chats`` (per-user JSON). This wrapper forwards to the same
    :class:`ChatManager` factory used by ``app.state.chat_manager_factory``.
    """

    def __init__(self, factory: Callable[[str], ChatManager]) -> None:
        self._factory = factory

    def _tenant_uid(self, explicit_user_id: Optional[str]) -> str:
        if copaw_storage_isolation_enabled():
            ctx = (get_context_user_id() or "").strip()
            if ctx:
                return ctx
        uid = (explicit_user_id or "").strip()
        return uid or DEFAULT_USER_ID

    def _spec_user_id(self, explicit_user_id: Optional[str]) -> str:
        return self._tenant_uid(explicit_user_id)

    async def list_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> list[ChatSpec]:
        mgr = self._factory(self._tenant_uid(user_id))
        return await mgr.list_chats(user_id=user_id, channel=channel)

    async def get_chat(self, chat_id: str) -> Optional[ChatSpec]:
        mgr = self._factory(self._tenant_uid(None))
        return await mgr.get_chat(chat_id)

    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
        name: str = "New Chat",
    ) -> ChatSpec:
        tid = self._tenant_uid(user_id)
        spec_uid = self._spec_user_id(user_id)
        mgr = self._factory(tid)
        return await mgr.get_or_create_chat(
            session_id,
            spec_uid,
            channel,
            name=name,
        )

    async def create_chat(self, spec: ChatSpec) -> ChatSpec:
        mgr = self._factory(self._tenant_uid(spec.user_id))
        return await mgr.create_chat(spec)

    async def update_chat(self, spec: ChatSpec) -> ChatSpec:
        mgr = self._factory(self._tenant_uid(spec.user_id))
        return await mgr.update_chat(spec)

    async def delete_chats(self, chat_ids: list[str]) -> bool:
        mgr = self._factory(self._tenant_uid(None))
        return await mgr.delete_chats(chat_ids)

    async def count_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> int:
        mgr = self._factory(self._tenant_uid(user_id))
        return await mgr.count_chats(user_id=user_id, channel=channel)

    async def get_chat_id_by_session(
        self,
        session_id: str,
        channel: str,
    ) -> str | None:
        mgr = self._factory(self._tenant_uid(None))
        return await mgr.get_chat_id_by_session(session_id, channel)
