"""очистка истёкших неиспользованных posting claims"""

import asyncio

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.repositories.posting_claims import PostingClaimsRepository


async def cleanup_posting_claims() -> None:
    """удалить только db-записи expired claims и их unattached media"""

    pool = await create_postgres_pool(get_settings())
    try:
        deleted = await PostingClaimsRepository(pool).cleanup_expired()
        print(f"deleted expired posting claims: {deleted}")
    finally:
        await close_postgres_pool(pool)


if __name__ == "__main__":
    asyncio.run(cleanup_posting_claims())
