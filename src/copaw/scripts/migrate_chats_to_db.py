#!/usr/bin/env python3
"""Migrate existing Copaw chat data from JSON files to TiDB.

Usage:
    PYTHONPATH=/app/copaw-source:/app/venv/lib/python3.11/site-packages \\
        python3 -m copaw.scripts.migrate_chats_to_db

Or specify a custom USERS_DIR:
    python3 -m copaw.scripts.migrate_chats_to_db --users-dir /path/to/users
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_chats")

DB_URL = "mysql+pymysql://root:@tidb:4000/lazycraft?charset=utf8mb4"


def get_users_dir(args_users_dir: str | None) -> Path:
    if args_users_dir:
        return Path(args_users_dir).expanduser().resolve()
    # Default: try Copaw's USERS_DIR
    try:
        from copaw.constant import USERS_DIR
        return Path(USERS_DIR).resolve()
    except ImportError:
        return Path.home() / ".copaw" / "users"


def migrate_user(conn, user_id: str, users_dir: Path) -> int:
    """Migrate one user's data. Returns chat count."""
    user_dir = users_dir / user_id
    if not user_dir.is_dir():
        return 0

    # --- chats.json → copaw_chat_sessions ---
    chats_path = user_dir / "chats.json"
    specs = []
    if chats_path.exists():
        data = json.loads(chats_path.read_text(encoding="utf-8"))
        specs = data.get("chats", [])
        for spec in specs:
            conn.execute(
                text(
                    "INSERT INTO copaw_chat_sessions "
                    "(id, session_id, name, user_id, channel, status, created_at, updated_at) "
                    "VALUES (:id, :sid, :name, :uid, :ch, :st, :ca, :ua) "
                    "ON DUPLICATE KEY UPDATE "
                    "session_id=VALUES(session_id), name=VALUES(name), "
                    "channel=VALUES(channel), status=VALUES(status), "
                    "updated_at=VALUES(updated_at)"
                ),
                {
                    "id": spec.get("id", ""),
                    "sid": spec.get("session_id", ""),
                    "name": spec.get("name", "New Chat"),
                    "uid": spec.get("user_id", user_id),
                    "ch": spec.get("channel", "console"),
                    "st": spec.get("status", "idle"),
                    "ca": _parse_dt(spec.get("created_at")),
                    "ua": _parse_dt(spec.get("updated_at")) or datetime.utcnow(),
                },
            )
        logger.info(f"  sessions: {len(specs)}")

    # --- sessions/*.json → copaw_chat_messages ---
    sessions_dir = user_dir / "sessions"
    msg_count = 0
    if sessions_dir.is_dir():
        for f in sorted(sessions_dir.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            agent_data = data.get("agent") or {}
            memory = agent_data.get("memory") or {}
            content_pairs = memory.get("content") or []
            session_id = f.stem.replace("--", ":")
            for pair in content_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) == 0:
                    continue
                for item in pair:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role", "")
                    if role == "system":
                        continue
                    texts = []
                    reasoning_text = ""
                    for c in (item.get("content") or []):
                        if isinstance(c, dict):
                            if c.get("type") == "text":
                                texts.append(c.get("text", ""))
                            elif c.get("type") == "thinking":
                                reasoning_text = c.get("thinking", "")
                    if not texts:
                        continue
                    content = "".join(texts)
                    conn.execute(
                        text(
                            "INSERT INTO copaw_chat_messages "
                            "(session_id, role, content, reasoning, created_at) "
                            "VALUES (:sid, :role, :content, :reasoning, :ca)"
                        ),
                        {
                            "sid": session_id,
                            "role": role,
                            "content": content,
                            "reasoning": reasoning_text,
                            "ca": _parse_dt(item.get("timestamp")),
                        },
                    )
                    msg_count += 1
        logger.info(f"  messages: {msg_count}")

    return len(specs)


def _parse_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Migrate Copaw chat data to TiDB")
    parser.add_argument("--users-dir", help="Override USERS_DIR path")
    parser.add_argument("--user-id", help="Only migrate a specific user")
    args = parser.parse_args()

    users_dir = get_users_dir(args.users_dir)
    if not users_dir.is_dir():
        logger.error(f"USERS_DIR not found: {users_dir}")
        sys.exit(1)

    engine = create_engine(DB_URL, pool_size=5, max_overflow=5)
    total_chats = 0

    user_ids = [args.user_id] if args.user_id else sorted(
        d.name for d in users_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )

    with engine.begin() as conn:
        for uid in user_ids:
            logger.info(f"Migrating user: {uid}")
            n = migrate_user(conn, uid, users_dir)
            total_chats += n

    logger.info(f"Done. Migrated {total_chats} chat sessions total.")


if __name__ == "__main__":
    main()
