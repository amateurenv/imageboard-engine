"""зависимости для HTTP-маршрутов"""

from typing import Annotated

from asyncpg import Pool
from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.site_settings import SiteSettings
from app.repositories.threads import ThreadsRepository
from app.services.threads import ThreadsService


def get_postgres_pool(request: Request) -> Pool:
    """вернуть общий пул подключений к postgresql"""

    # пул создаётся при запуске fastapi и хранится в состоянии приложения
    return request.app.state.postgres_pool


def get_redis_client(request: Request) -> Redis:
    """вернуть общий клиент redis"""

    # клиент создаётся при запуске fastapi и хранится в состоянии приложения
    return request.app.state.redis_client


def get_site_settings(request: Request) -> SiteSettings:
    """вернуть проверенные настройки сайта"""

    # настройки загружаются из postgresql один раз при запуске
    return request.app.state.site_settings


# эти короткие имена будут использоваться в параметрах маршрутов
type PostgresPoolDependency = Annotated[
    Pool,
    Depends(get_postgres_pool),
]

type RedisClientDependency = Annotated[
    Redis,
    Depends(get_redis_client),
]

type SiteSettingsDependency = Annotated[
    SiteSettings,
    Depends(get_site_settings),
]


def get_threads_service(
    postgres_pool: PostgresPoolDependency,
    site_settings: SiteSettingsDependency,
) -> ThreadsService:
    """создать service для одного http-запроса"""

    # эти объекты лёгкие и не создают новое подключение к postgresql
    repository = ThreadsRepository(postgres_pool)

    return ThreadsService(repository, site_settings)


type ThreadsServiceDependency = Annotated[
    ThreadsService,
    Depends(get_threads_service),
]