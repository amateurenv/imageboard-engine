"""точка входа asgi приложения"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.system import router as system_router
from app.api.v1.router import router as api_v1_router
from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.core.site_settings_repository import load_site_settings
from app.ws.manager import WebSocketManager

# получаем проверенные настройки один раз для всего процесса
settings = get_settings()

@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """создать подключения при старте и закрыть при остановке"""

    # создаём клиент redis, но соединение откроется при первом запросе
    redis_client = create_redis_client(settings)

    try:
        # проверяем redis до того, как приложение начнёт принимать запросы
        await redis_client.ping()

        # сохраняем клиент, чтобы маршруты могли его использовать
        application.state.redis_client = redis_client

        # создаём общий пул подключений к postgresql
        application.state.postgres_pool = await create_postgres_pool(settings)

        # загружаем и проверяем настройки сайта из postgresql
        application.state.site_settings = await load_site_settings(
            application.state.postgres_pool
        )
    except Exception:
        # redis нужно закрыть даже при неудачном запуске
        await close_redis_client(redis_client)
        raise

    # websocket-менеджер и задача потребления событий из redis
    ws_manager = WebSocketManager(redis_client)
    application.state.ws_manager = ws_manager
    ws_consumer = asyncio.create_task(ws_manager.consume_redis())

    try:
        # после yield fastapi начинает принимать запросы
        yield
    finally:
        # останавливаем потребление redis-событий
        ws_consumer.cancel()
        # закрываем подключения при штатной остановке или Ctrl+C
        await close_postgres_pool(application.state.postgres_pool)
        await close_redis_client(application.state.redis_client)


# создаём единственный объект fastapi для всего приложения
app = FastAPI(
    # название в интерактивной документации /docs
    title=settings.app_name,

    # подробные ошибки включаются только через APP_DEBUG=true
    debug=settings.app_debug,

    # версия нашего приложения, а не версия fastapi
    version="0.1.0",

    # функция запуска и остановки инфраструктуры
    lifespan=lifespan,
)


# подключаем служебные маршруты без версии
app.include_router(system_router)

# подключаем первую версию публичного API
app.include_router(api_v1_router)