"""удаление истёкших staff-сессий"""

import asyncio

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.repositories.users import UsersRepository


async def cleanup_sessions() -> None:
    """удалить истёкшие сессии и вывести число записей"""

    pool = await create_postgres_pool(get_settings())
    try:
        deleted = await UsersRepository(pool).delete_expired_sessions()
        print(f"deleted expired sessions: {deleted}")
    finally:
        await close_postgres_pool(pool)


if __name__ == "__main__":
    asyncio.run(cleanup_sessions())
