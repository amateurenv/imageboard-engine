"""точка входа asgi приложения"""

# AsyncIterator описывает асинхронный lifecycle
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# fastapi создаёт приложение и маршруты
from fastapi import FastAPI, Request

# BaseModel проверяет форму json ответа
from pydantic import BaseModel

# читаем проверенные настройки из .env
from app.core.config import get_settings

# функции для пула postgresql
from app.core.database import close_postgres_pool, create_postgres_pool

# функции для redis
from app.core.redis import close_redis_client, create_redis_client

# получаем настройки один раз
settings = get_settings()


# lifecycle запускается вместе с приложением
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """создать подключения при старте и закрыть при остановке"""

    # создаём redis клиент
    redis_client = create_redis_client(settings)

    try:
        # проверяем, что redis отвечает
        await redis_client.ping()

        # сохраняем redis клиент для маршрутов
        application.state.redis_client = redis_client

        # создаём пул postgresql один раз для всего приложения
        application.state.postgres_pool = await create_postgres_pool(settings)
    except Exception:
        # если запуск не удался, redis клиент всё равно надо закрыть
        await close_redis_client(redis_client)
        raise

    try:
        # после yield fastapi начинает принимать запросы
        yield
    finally:
        # закрываем подключения при остановке
        await close_postgres_pool(application.state.postgres_pool)
        await close_redis_client(application.state.redis_client)


# главный объект fastapi
app = FastAPI(
    # название в /docs
    title=settings.app_name,

    # подробные ошибки только в development
    debug=settings.app_debug,

    # версия нашего приложения
    version="0.1.0",

    # функция запуска и остановки подключений
    lifespan=lifespan,
)


# форма json ответа для /health
class HealthResponse(BaseModel):
    """ответ маршрута /health"""

    # строковый статус
    status: str

    # текущий режим приложения
    environment: str


# форма json ответа для /ready
class ReadyResponse(BaseModel):
    """ответ маршрута /ready"""

    # строковый статус
    status: str


# GET запрос на /health
@app.get("/health", response_model=HealthResponse, tags=["system"])
async def healthcheck() -> HealthResponse:
    """вернуть статус запущенного fastapi"""

    return HealthResponse(status="ok", environment=settings.app_env)


# GET запрос на /ready
@app.get("/ready", response_model=ReadyResponse, tags=["system"])
async def readiness(request: Request) -> ReadyResponse:
    """проверить postgresql и redis"""

    # берём одно соединение из пула и делаем простой sql запрос
    async with request.app.state.postgres_pool.acquire() as connection:
        await connection.fetchval("SELECT 1")

    # redis должен ответить PONG
    await request.app.state.redis_client.ping()

    return ReadyResponse(status="ok")