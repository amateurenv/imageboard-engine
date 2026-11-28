"""точка входа asgi приложения"""

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from asyncpg import Pool
from fastapi import FastAPI

from app.api.admin_ui import router as admin_ui_router
from app.api.media_delivery import router as media_delivery_router
from app.api.system import router as system_router
from app.api.v1.router import router as api_v1_router
from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.core.site_settings_repository import load_site_settings
from app.services.media_quota import MediaQuota
from app.services.media_storage import MediaStorage
from app.ws.manager import DisabledWebSocketManager

# получаем проверенные настройки один раз для всего процесса
settings = get_settings()

@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """создать подключения при старте и закрыть при остановке"""

    # создаём клиент redis, но соединение откроется при первом запросе
    redis_client = create_redis_client(settings)
    postgres_pool: Pool | None = None

    try:
        # проверяем redis до того, как приложение начнёт принимать запросы
        await redis_client.ping()

        # сохраняем клиент, чтобы маршруты могли его использовать
        application.state.redis_client = redis_client

        # создаём общий пул подключений к postgresql
        postgres_pool = await create_postgres_pool(settings)
        application.state.postgres_pool = postgres_pool

        # загружаем и проверяем настройки сайта из postgresql
        application.state.site_settings = await load_site_settings(
            application.state.postgres_pool
        )
        # upload и worker используют один закрытый корень, который пока не раздаётся наружу
        application.state.media_storage_root = settings.media_storage_root
        storage = MediaStorage(settings.media_storage_root)
        quota = MediaQuota(postgres_pool, storage, settings)
        application.state.media_quota = quota
        spool_probe = storage.resolve(".spool/ready")
        if settings.app_env != "production" or storage.root.is_mount():
            spool_probe = storage.prepare_parent(".spool/ready")
        tempfile.tempdir = str(spool_probe.parent)
    except Exception:
        # закрываем уже созданные подключения при неудачном запуске
        if postgres_pool is not None:
            await close_postgres_pool(postgres_pool)
        await close_redis_client(redis_client)
        raise

    # realtime временно выключен до отдельной безопасной переработки
    application.state.ws_manager = DisabledWebSocketManager()

    try:
        # после yield fastapi начинает принимать запросы
        yield
    finally:
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
# локальная админ-страница использует те же защищённые api-маршруты
app.include_router(admin_ui_router)
# этот маршрут возвращает только X-Accel-Redirect, байты отдаёт internal nginx
app.include_router(media_delivery_router)

# подключаем первую версию публичного API
app.include_router(api_v1_router)
