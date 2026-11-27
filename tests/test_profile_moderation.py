"""проверка owner-модерации profile-досок без внешней базы"""

# ruff: noqa: E501 - компактные state fixtures удобнее читать целыми

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from asyncpg import Pool

from app.repositories.users import UserRecord
from app.services.profile_moderation import ProfileModerationError, ProfileModerationService


class FakeConnection:
    """маленькая транзакционная модель досок, тредов, постов и audit log"""

    def __init__(self) -> None:
        self.boards = {
            "alice": {"id": 1, "board_type": "profile", "owner_user_id": 10},
            "bob": {"id": 2, "board_type": "profile", "owner_user_id": 20},
            "news": {"id": 3, "board_type": "public", "owner_user_id": None},
        }
        self.threads = {
            101: {"board": "alice", "is_sticky": False, "is_locked": False, "is_hidden": False},
            201: {"board": "bob", "is_sticky": False, "is_locked": False, "is_hidden": False},
            301: {"board": "news", "is_sticky": False, "is_locked": False, "is_hidden": False},
        }
        self.posts = {1001: {"thread": 101, "is_hidden": False}, 1002: {"thread": 101, "is_hidden": False}}
        self.logs: list[dict[str, object]] = []
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transactions += 1
        yield

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "JOIN posts" in query:
            post = self.posts.get(cast(int, args[1]))
            if post is None or self.threads[post["thread"]]["board"] != args[0]:
                return None
            return self.boards[cast(str, args[0])]
        if "JOIN threads" in query:
            thread = self.threads.get(cast(int, args[1]))
            if thread is None or thread["board"] != args[0]:
                return None
            return self.boards[cast(str, args[0])]
        if "SELECT is_sticky" in query:
            return self.threads.get(cast(int, args[0]))
        return self.boards.get(cast(str, args[0]))

    async def fetchval(self, query: str, *args: object) -> int | None:
        target = cast(int, args[0])
        if "UPDATE posts" in query:
            self.posts[target]["is_hidden"] = args[1]
            return target
        if "DELETE FROM threads" in query:
            return target if self.threads.pop(target, None) is not None else None
        post = self.posts.get(target)
        if post is None:
            return None
        if target == 1001:
            return None
        self.posts.pop(target)
        return target

    async def execute(self, query: str, *args: object) -> str:
        if "UPDATE threads" in query:
            thread = self.threads[cast(int, args[0])]
            thread.update(is_sticky=args[1], is_locked=args[2], is_hidden=args[3])
        elif "UPDATE posts" in query:
            self.posts[cast(int, args[0])]["is_hidden"] = args[1]
        elif "INSERT INTO moderation_log" in query:
            self.logs.append({"actor_id": args[0], "action": args[1], "target_id": args[3], "metadata": args[5]})
        return "UPDATE 1"


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        yield self.connection


def user(user_id: int, role: str = "user") -> UserRecord:
    """дать тесту обычного владельца или staff"""

    return UserRecord(user_id, "tester", role, datetime.now(UTC))


@pytest.mark.anyio
async def test_owner_can_change_thread_states_and_log_metadata() -> None:
    """sticky, lock и hide меняются в одной транзакции с журналом"""

    pool = FakePool()
    service = ProfileModerationService(cast(Pool, pool))
    assert await service.update_thread("alice", 101, user(10), is_sticky=True, is_locked=True, is_hidden=True)
    assert pool.connection.threads[101] == {"board": "alice", "is_sticky": True, "is_locked": True, "is_hidden": True}
    assert await service.update_thread("alice", 101, user(10), is_sticky=False, is_locked=False, is_hidden=False)
    metadata = cast(str, pool.connection.logs[-1]["metadata"])
    assert "profile_owner" in metadata and "board_id" in metadata and "cookie" not in metadata
    assert pool.connection.transactions == len(pool.connection.logs)


@pytest.mark.anyio
async def test_owner_can_hide_reply_and_delete_only_non_op() -> None:
    """hide обратим, а OP сохраняет существующее правило удаления"""

    pool = FakePool()
    service = ProfileModerationService(cast(Pool, pool))
    assert await service.update_post("alice", 1002, user(10), True)
    assert await service.update_post("alice", 1002, user(10), False)
    assert await service.delete_post("alice", 1002, user(10))
    with pytest.raises(ValueError, match="opening post"):
        await service.delete_post("alice", 1001, user(10))


@pytest.mark.anyio
async def test_owner_cannot_cross_profile_or_public_and_staff_overrides() -> None:
    """handle и id другой доски не обходят owner_user_id проверку"""

    service = ProfileModerationService(cast(Pool, FakePool()))
    for handle, thread_id in (("bob", 201), ("news", 301)):
        with pytest.raises(ProfileModerationError):
            await service.update_thread(handle, thread_id, user(10), is_locked=True)
    assert not await service.update_thread("alice", 201, user(10), is_locked=True)
    assert await service.update_thread("news", 301, user(99, "moderator"), is_locked=True)


@pytest.mark.anyio
async def test_owner_delete_thread_uses_cascade_target() -> None:
    """удаление треда идёт через одну owner-проверку и audit log"""

    pool = FakePool()
    service = ProfileModerationService(cast(Pool, pool))
    assert await service.delete_thread("alice", 101, user(10))
    assert 101 not in pool.connection.threads
    assert pool.connection.logs[-1]["action"] == "delete_thread"
