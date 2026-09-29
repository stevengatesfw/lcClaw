# -*- coding: utf-8 -*-
"""Regression tests for keeping one chat session across edits and deletes."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from copaw.app.runner.edit_resend import (
    EDIT_RESEND_KEEP_USER_TURNS_META,
    delete_user_turns,
    edit_resend_keep_user_turns,
    normalize_user_turn_indices,
    truncate_memory_after_user_turns,
)


@dataclass
class FakeMessage:
    id: str
    role: str


class FakeMemory:
    def __init__(self, roles: list[str]) -> None:
        self.content = [
            (FakeMessage(id=f"message-{index}", role=role), set())
            for index, role in enumerate(roles)
        ]

    async def delete(self, message_ids: list[str]) -> int:
        before = len(self.content)
        ids = set(message_ids)
        self.content = [
            entry for entry in self.content if entry[0].id not in ids
        ]
        return before - len(self.content)


async def test_truncate_keeps_same_conversation_prefix() -> None:
    memory = FakeMemory([
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ])

    removed = await truncate_memory_after_user_turns(memory, 1)

    assert removed == 3
    assert [entry[0].role for entry in memory.content] == [
        "system",
        "user",
        "assistant",
    ]


async def test_truncate_without_matching_boundary_keeps_memory() -> None:
    """状态丢失/被压缩/已截断过时不该报错，本轮照常进行。"""
    memory = FakeMemory(["system", "user", "assistant"])

    removed = await truncate_memory_after_user_turns(memory, 1)

    assert removed == 0
    assert [entry[0].role for entry in memory.content] == [
        "system",
        "user",
        "assistant",
    ]


async def test_truncate_on_empty_memory_is_a_noop() -> None:
    memory = FakeMemory([])

    assert await truncate_memory_after_user_turns(memory, 0) == 0
    assert memory.content == []


async def test_truncate_rejects_negative_boundary() -> None:
    memory = FakeMemory(["user"])

    with pytest.raises(ValueError, match="non-negative"):
        await truncate_memory_after_user_turns(memory, -1)


def test_edit_resend_boundary_validation() -> None:
    key = EDIT_RESEND_KEEP_USER_TURNS_META
    assert edit_resend_keep_user_turns({}) is None
    assert edit_resend_keep_user_turns({key: 0}) == 0
    with pytest.raises(ValueError, match="invalid"):
        edit_resend_keep_user_turns({key: True})


def _ids(memory: FakeMemory) -> list[str]:
    return [entry[0].id for entry in memory.content]


async def test_delete_middle_turn_keeps_later_turns() -> None:
    """删中间一轮不能连带后面没删的轮次。"""
    memory = FakeMemory([
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ])

    removed = await delete_user_turns(memory, [1])

    assert removed == 3
    assert _ids(memory) == [
        "message-0",
        "message-1",
        "message-5",
        "message-6",
    ]


async def test_delete_multiple_turns_at_once() -> None:
    memory = FakeMemory(["user", "assistant", "user", "assistant"])

    removed = await delete_user_turns(memory, [0, 1])

    assert removed == 4
    assert memory.content == []


async def test_delete_out_of_range_turn_is_ignored() -> None:
    """客户端视图过期时按「无可删」处理，不报错。"""
    memory = FakeMemory(["user", "assistant"])

    assert await delete_user_turns(memory, [3]) == 0
    assert _ids(memory) == ["message-0", "message-1"]

    assert await delete_user_turns(memory, []) == 0


def test_normalize_user_turn_indices() -> None:
    assert normalize_user_turn_indices([2, 0, 2]) == [2, 0]
    assert normalize_user_turn_indices([]) == []
    for bad in ([True], [-1], ["0"], [None]):
        with pytest.raises(ValueError, match="invalid user-turn index"):
            normalize_user_turn_indices(bad)
