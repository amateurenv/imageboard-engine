"""проверка маршрута /ready"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


# соединение postgresql для теста
class FakeConnection:
    # noinspection PyMethodMayBeStatic
    async def fetchval(self, query: str) -> int:
        # маршрут должен выполнить именно этот простой запрос
        assert query == "SELECT 1"
        return 1


# маленькая версия пула postgresql для теста
class FakePool:
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        yield FakeConnection()


# маленькая версия redis для теста
class FakeRedis:
    # noinspection PyMethodMayBeStatic
    async def ping(self) -> bool:
        return True


@pytest.mark.anyio
async def test_readiness_returns_ok() -> None:
    """маршрут /ready возвращает ok если сервисы отвечают"""

    # вместо настоящих сервисов даём маршруту тестовые версии
    app.state.postgres_pool = FakePool()
    app.state.redis_client = FakeRedis()

    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}