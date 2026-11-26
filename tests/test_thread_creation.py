"""проверка создания треда вместе с первым постом"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from asyncpg import Pool

from app.repositories.threads import CREATE_POST_SQL, CREATE_THREAD_SQL, ThreadsRepository


class FakeConnection:
    """соединение postgresql для проверки одной транзакции"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """обозначить границы транзакции"""

        yield

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        """вернуть тестовые строки нового треда и поста"""

        self.calls.append((query, args))

        if query == CREATE_THREAD_SQL:
            return {
                "id": 12,
                "subject": "тест",
                "is_sticky": False,
                "is_locked": False,
                "is_archived": False,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "bumped_at": datetime(2026, 1, 1, tzinfo=UTC),
            }

        if query == CREATE_POST_SQL:
            return {
                "id": 13,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }

        raise AssertionError(f"unexpected query: {query}")


class FakePool:
    """пул с одним тестовым соединением"""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        """выдать одно тестовое соединение"""

        yield self.connection


@pytest.mark.anyio
async def test_create_thread_creates_opening_post_in_same_transaction() -> None:
    """тред и его первый пост сохраняются вместе"""

    connection = FakeConnection()
    repository = ThreadsRepository(cast(Pool, FakePool(connection)))

    result = await repository.create_thread(
        board_id=5,
        subject="тест",
        author_name="автор",
        body="первый пост",
    )

    assert result.thread.id == 12
    assert result.thread.is_sticky is False
    assert result.post.id == 13
    assert result.post.body == "первый пост"
    assert connection.calls == [
        (CREATE_THREAD_SQL, (5, "тест", False)),
        (CREATE_POST_SQL, (12, "автор", "первый пост")),
    ]
