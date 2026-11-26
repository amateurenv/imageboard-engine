"""проверка атомарного создания постов"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from asyncpg import Pool

from app.repositories.threads import (
    BUMP_THREAD_SQL,
    CREATE_POST_SQL,
    LOCK_THREAD_FOR_POST_SQL,
    ThreadsRepository,
)


class FakeConnection:
    """соединение postgresql с заранее заданным состоянием треда"""

    def __init__(self, thread_state: dict[str, bool]) -> None:
        self.thread_state = thread_state
        self.queries: list[str] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """показать, что действия выполняются внутри транзакции"""

        yield

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        """вернуть строку треда или нового поста"""

        self.queries.append(query)

        if query == LOCK_THREAD_FOR_POST_SQL:
            return self.thread_state

        if query == CREATE_POST_SQL:
            return {
                "id": 15,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }

        raise AssertionError(f"unexpected fetchrow query: {query}")

    # noinspection PyMethodMayBeStatic
    async def fetch(self, query: str, *args: object) -> list[dict[str, int]]:
        """вернуть пустой список ссылок для простого теста"""

        return []

    async def execute(self, query: str, *args: object) -> str:
        """запомнить запрос обновления bump времени"""

        self.queries.append(query)
        return "UPDATE 1"


class FakePool:
    """маленький пул с одним тестовым соединением"""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        """выдать тестовое соединение"""

        yield self.connection


def make_repository(thread_state: dict[str, bool]) -> tuple[ThreadsRepository, FakeConnection]:
    """создать repository с тестовым пулом"""

    connection = FakeConnection(thread_state)
    fake_pool = FakePool(connection)
    repository = ThreadsRepository(cast(Pool, fake_pool))

    return repository, connection


@pytest.mark.anyio
async def test_create_post_rejects_locked_thread_before_insert() -> None:
    """закрытый тред не получает новый пост"""

    repository, connection = make_repository(
        {
            "is_locked": True,
            "is_archived": False,
        }
    )

    with pytest.raises(ValueError, match="thread is locked"):
        await repository.create_post(
            thread_id=7,
            author_name=None,
            body="тест",
            reference_ids=[],
        )

    assert CREATE_POST_SQL not in connection.queries
    assert BUMP_THREAD_SQL not in connection.queries


@pytest.mark.anyio
async def test_create_post_rejects_archived_thread_before_insert() -> None:
    """архивный тред не получает новый пост"""

    repository, connection = make_repository(
        {
            "is_locked": False,
            "is_archived": True,
        }
    )

    with pytest.raises(ValueError, match="thread is archived"):
        await repository.create_post(
            thread_id=7,
            author_name=None,
            body="тест",
            reference_ids=[],
        )

    assert CREATE_POST_SQL not in connection.queries
    assert BUMP_THREAD_SQL not in connection.queries


@pytest.mark.anyio
async def test_create_post_inserts_and_bumps_available_thread() -> None:
    """доступный тред получает пост и bump в одной транзакции"""

    repository, connection = make_repository(
        {
            "is_locked": False,
            "is_archived": False,
        }
    )

    result = await repository.create_post(
        thread_id=7,
        author_name=None,
        body="тест",
        reference_ids=[],
    )

    assert result is not None
    assert result.post.id == 15
    assert result.was_bumped is True
    assert connection.queries == [
        LOCK_THREAD_FOR_POST_SQL,
        CREATE_POST_SQL,
        BUMP_THREAD_SQL,
    ]
