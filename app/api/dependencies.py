"""зависимости для HTTP-маршрутов"""

from typing import Annotated

from asyncpg import Pool
from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.site_settings import SiteSettings


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