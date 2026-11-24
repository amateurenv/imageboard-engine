"""правила защиты от спама"""

from fastapi import HTTPException
from redis.asyncio import Redis

# максимум новых тредов на доску за сутки
MAX_THREADS_PER_BOARD_DAY = 3

# максимум постов в треде за час
MAX_POSTS_PER_THREAD_HOUR = 10

# окна действия лимитов в секундах
THREADS_WINDOW_SECONDS = 24 * 60 * 60
POSTS_WINDOW_SECONDS = 60 * 60


class SpamService:
    """rate limit создания тредов и постов через redis"""

    def __init__(self, redis_client: Redis) -> None:
        self._redis_client = redis_client

    async def check_thread_limit(self, board_slug: str) -> None:
        """проверить, не превышен ли лимит создания тредов на доску"""

        await self._check_limit(
            f"spam:threads:{board_slug}",
            MAX_THREADS_PER_BOARD_DAY,
            THREADS_WINDOW_SECONDS,
            f"thread creation limit exceeded on board {board_slug}",
        )

    async def check_post_limit(self, thread_id: int) -> None:
        """проверить, не превышен ли лимит постов в треде"""

        await self._check_limit(
            f"spam:posts:{thread_id}",
            MAX_POSTS_PER_THREAD_HOUR,
            POSTS_WINDOW_SECONDS,
            f"post creation limit exceeded in thread {thread_id}",
        )

    async def _check_limit(self, key: str, limit: int, ttl: int, detail: str) -> None:
        """проверить и увеличить счётчик, при превышении превышения — 429"""

        current = await self._redis_client.get(key)
        if current is not None and int(current) >= limit:
            raise HTTPException(status_code=429, detail=detail)

        await self._redis_client.incr(key)
        await self._redis_client.expire(key, ttl)
