# -*- coding: utf-8 -*-
"""TiDB-based chat repository."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text, MetaData, Table, Column, String, Boolean, BigInteger, Text, DateTime
from sqlalchemy.engine import Engine

from .base import BaseChatRepository
from ..models import ChatSpec, ChatsFile
from ...channels.schema import DEFAULT_CHANNEL

_engine: Engine | None = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            "mysql+pymysql://root:@tidb:4000/lazycraft?charset=utf8mb4",
            pool_size=5,
            max_overflow=5,
            pool_recycle=3600,
        )
    return _engine


class DbChatRepository(BaseChatRepository):
    """TiDB-backed chat repository (single-user)."""

    def __init__(self, user_id: str):
        self._user_id = user_id
        self._engine = _get_engine()

    async def load(self) -> ChatsFile:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, session_id, name, user_id, channel, "
                    "status, created_at, updated_at "
                    "FROM copaw_chat_sessions WHERE user_id = :uid"
                ),
                {"uid": self._user_id},
            ).fetchall()

        chats = []
        for row in rows:
            chats.append(ChatSpec(
                id=row[0],
                session_id=row[1],
                name=row[2] or "New Chat",
                user_id=row[3],
                channel=row[4] or DEFAULT_CHANNEL,
                status=row[5] or "idle",
                created_at=self._to_dt(row[6]),
                updated_at=self._to_dt(row[7]),
            ))
        return ChatsFile(version=1, chats=chats)

    async def save(self, chats_file: ChatsFile) -> None:
        with self._engine.begin() as conn:
            for spec in chats_file.chats:
                conn.execute(
                    text(
                        "INSERT INTO copaw_chat_sessions "
                        "(id, session_id, name, user_id, channel, status, created_at, updated_at) "
                        "VALUES (:id, :sid, :name, :uid, :ch, :st, :ca, :ua) "
                        "ON DUPLICATE KEY UPDATE "
                        "session_id=VALUES(session_id), name=VALUES(name), user_id=VALUES(user_id), "
                        "channel=VALUES(channel), status=VALUES(status), "
                        "updated_at=VALUES(updated_at)"
                    ),
                    {
                        "id": spec.id,
                        "sid": spec.session_id,
                        "name": spec.name,
                        "uid": spec.user_id,
                        "ch": spec.channel,
                        "st": spec.status,
                        "ca": spec.created_at,
                        "ua": spec.updated_at or datetime.utcnow(),
                    },
                )

            # Delete rows that were removed from the file
            if chats_file.chats:
                ids = [s.id for s in chats_file.chats]
                conn.execute(
                    text(
                        "DELETE FROM copaw_chat_sessions "
                        "WHERE user_id = :uid AND id NOT IN :ids"
                    ),
                    {"uid": self._user_id, "ids": ids},
                )
            else:
                conn.execute(
                    text("DELETE FROM copaw_chat_sessions WHERE user_id = :uid"),
                    {"uid": self._user_id},
                )

    @staticmethod
    def _to_dt(val) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(str(val))


async def sync_messages_to_db(session_id: str, user_id: str, state_dicts: dict) -> None:
    """Sync messages from agent state dict to copaw_chat_messages table."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        memory = state_dicts.get("agent", {}).get("memory", {})
        content_pairs = memory.get("content") or []
        if not content_pairs:
            return

        rows = []
        for pair in content_pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) == 0:
                continue
            msg = pair[0] if isinstance(pair[0], dict) else {}
            role = msg.get("role", "")
            if role == "system":
                continue
            msg_content = msg.get("content") or ""
            if isinstance(msg_content, list):
                parts = []
                for b in msg_content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b.get("text", ""))
                msg_content = " ".join(parts)
            if not msg_content:
                continue
            timestamp = _parse_msg_dt(msg.get("timestamp"))
            rows.append({
                "sid": session_id,
                "role": role,
                "content": str(msg_content),
                "ts": timestamp,
            })

        if not rows:
            return

        from sqlalchemy import text as sa_text
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                sa_text("DELETE FROM copaw_chat_messages WHERE session_id = :sid"),
                {"sid": session_id},
            )
            for r in rows:
                conn.execute(
                    sa_text(
                        "INSERT INTO copaw_chat_messages "
                        "(session_id, role, content, created_at) "
                        "VALUES (:sid, :role, :content, :ts)"
                    ),
                    {"sid": r["sid"], "role": r["role"], "content": r["content"], "ts": r["ts"]},
                )
        logger.debug("Synced %d messages for session %s", len(rows), session_id)
    except Exception:
        logger.warning("sync_messages_to_db failed for session %s", session_id, exc_info=True)


def _parse_msg_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
