# -*- coding: utf-8 -*-
"""Helpers for pruning user turns without changing the chat session.

The console "edit and resend" and "delete turn" actions both used to abandon
the session for a fresh one, because CoPaw had no way to drop a turn from the
persisted memory.  These helpers do it in place instead, so the chat keeps its
``session_id`` and stays a single entry in the history list.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


EDIT_RESEND_KEEP_USER_TURNS_META = "lcagent_edit_resend_keep_user_turns"


def edit_resend_keep_user_turns(meta: dict[str, Any]) -> int | None:
    """Return the validated edit-resend boundary from request metadata."""
    raw = meta.get(EDIT_RESEND_KEEP_USER_TURNS_META)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("invalid edit-resend user-turn boundary")
    return raw


def normalize_user_turn_indices(indices: Iterable[Any]) -> list[int]:
    """Validate client-supplied user-turn positions, dropping duplicates."""
    normalized: list[int] = []
    for raw in indices:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("invalid user-turn index")
        if raw not in normalized:
            normalized.append(raw)
    return normalized


def _iter_memory_messages(memory: Any) -> Iterator[tuple[int, Any]]:
    """Yield ``(content index, message)`` for well-formed memory entries."""
    for index, entry in enumerate(memory.content):
        if isinstance(entry, (list, tuple)) and entry:
            yield index, entry[0]


async def _delete_memory_indices(memory: Any, indices: Iterable[int]) -> int:
    """Delete the memory entries at ``indices``; return how many went."""
    message_ids = []
    for index in sorted(set(indices)):
        entry = memory.content[index]
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        message_id = getattr(entry[0], "id", None)
        if message_id:
            message_ids.append(message_id)
    return await memory.delete(message_ids) if message_ids else 0


async def truncate_memory_after_user_turns(
    memory: Any,
    keep_user_turns: int,
) -> int:
    """Remove the selected user turn and everything after it from memory.

    ``keep_user_turns`` is the number of user messages that must remain.  The
    next user message is the one being replaced.  System messages and all
    earlier assistant/tool messages remain attached to the same session.

    Memory that holds no such user message needs no truncation: the state may
    have been dropped, compacted, or truncated by an earlier resend.  Returns
    ``0`` in that case so the turn still runs instead of failing the request.
    """
    if keep_user_turns < 0:
        raise ValueError("keep_user_turns must be non-negative")

    user_turns = 0
    for index, message in _iter_memory_messages(memory):
        if getattr(message, "role", None) != "user":
            continue
        if user_turns == keep_user_turns:
            return await _delete_memory_indices(
                memory,
                range(index, len(memory.content)),
            )
        user_turns += 1

    return 0


async def delete_user_turns(
    memory: Any,
    user_turn_indices: Iterable[int],
) -> int:
    """Delete whole user turns, each together with the replies following it.

    ``user_turn_indices`` are 0-based positions among the *user* messages in
    memory.  A turn spans its user message plus every following non-user
    message, up to the next user message; turns the client did not select stay
    untouched, so deleting a middle turn does not cost the later ones.

    Positions past the end of memory are ignored: a stale client view then
    prunes nothing rather than failing the request.
    """
    wanted = set(user_turn_indices)
    if not wanted:
        return 0

    indices: list[int] = []
    user_turns = -1
    selected = False
    for index, message in _iter_memory_messages(memory):
        if getattr(message, "role", None) != "user":
            if selected:
                indices.append(index)
            continue
        user_turns += 1
        selected = user_turns in wanted
        if selected:
            indices.append(index)

    return await _delete_memory_indices(memory, indices)
