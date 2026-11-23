"""пул подключений postgresql"""
from collections.abc import Awaitable
from typing import cast

import asyncpg
from asyncpg import Pool

from app.core.config import Settings


async def create_postgres_pool(settings: Settings) -> Pool:
    """создать небольшой пул подключений postgresql"""

    pool = asyncpg.create_pool(
        # адрес базы из .env, пароль не хранится в коде
        dsn=str(settings.database_url),

        # при старте достаточно одного соединения
        min_size=1,

        # больше десяти соединений одновременно не открывать
        max_size=10,

        # закрывать свободное соединение через пять минут
        max_inactive_connection_lifetime=300,

        # sql запрос ждёт ответ не дольше пяти секунд
        command_timeout=5,

        # имя приложения будет видно в postgresql
        server_settings={"application_name": "imageboard-engine"},
    )
    # cast говорит pycharm, что pool можно ожидать через await
    return await cast(Awaitable[Pool], pool)

async def close_postgres_pool(pool: Pool) -> None:
    """закрыть соединения пула при остановке приложения"""

    await pool.close()
