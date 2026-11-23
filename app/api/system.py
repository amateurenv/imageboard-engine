"""служебные маршруты приложения"""

from fastapi import APIRouter

from app.api.dependencies import (
    PostgresPoolDependency,
    RedisClientDependency,
)
from app.core.config import get_settings
from app.schemas.system import HealthResponse, ReadyResponse

# router собирает связанные HTTP-маршруты в одном модуле
router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def healthcheck() -> HealthResponse:
    """вернуть статус запущенного приложения"""

    # get_settings возвращает уже проверенные настройки из кэша
    settings = get_settings()

    return HealthResponse(
        status="ok",
        environment=settings.app_env,
    )


@router.get("/ready", response_model=ReadyResponse)
async def readiness(
    postgres_pool: PostgresPoolDependency,
    redis_client: RedisClientDependency,
) -> ReadyResponse:
    """проверить postgresql и redis"""

    # берём соединение из пула и выполняем лёгкий запрос к postgresql
    async with postgres_pool.acquire() as connection:
        await connection.fetchval("SELECT 1")

    # redis должен ответить PONG
    await redis_client.ping()

    return ReadyResponse(status="ok")