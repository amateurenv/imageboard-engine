"""правила защиты от спама"""

from fastapi import HTTPException
from redis.asyncio import Redis

from app.core.site_settings import SiteSettings

# окна действия лимитов в секундах
THREADS_WINDOW_SECONDS = 24 * 60 * 60
POSTS_WINDOW_SECONDS = 60 * 60
DELETION_PASSWORD_WINDOW_SECONDS = 60 * 60
DELETION_PASSWORD_MAX_ATTEMPTS = 5
MEDIA_UPLOAD_WINDOW_SECONDS = 60 * 60
MEDIA_UPLOAD_MAX_ATTEMPTS = 20

# lua выполняется в redis целиком, поэтому параллельные запросы не обходят лимит
CHECK_AND_INCREMENT_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[2])

if current >= limit then
    return 0
end

current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end

return 1
"""


class SpamService:
    """rate limit создания тредов и постов через redis"""

    def __init__(self, redis_client: Redis, site_settings: SiteSettings) -> None:
        self._redis_client = redis_client
        self._site_settings = site_settings

    async def check_thread_limit(self, client_ip: str, board_slug: str) -> None:
        """проверить, не превышен ли лимит создания тредов на доску"""

        await self._check_limit(
            f"spam:threads:{board_slug}:{client_ip}",
            self._site_settings.thread.max_threads_per_board_day,
            THREADS_WINDOW_SECONDS,
            "thread creation limit exceeded",
        )

    async def check_post_limit(self, client_ip: str, thread_id: int) -> None:
        """проверить, не превышен ли лимит постов в треде"""

        await self._check_limit(
            f"spam:posts:{thread_id}:{client_ip}",
            self._site_settings.post.max_posts_per_thread_hour,
            POSTS_WINDOW_SECONDS,
            "post creation limit exceeded",
        )

    async def check_deletion_password_limit(self, client_ip: str, target_id: int) -> None:
        """ограничить перебор пароля удаления для одного объекта"""

        await self._check_limit(
            f"spam:deletion_password:{target_id}:{client_ip}",
            DELETION_PASSWORD_MAX_ATTEMPTS,
            DELETION_PASSWORD_WINDOW_SECONDS,
            "deletion password rate limit exceeded",
        )

    async def check_media_upload_limit(self, client_ip: str, board_slug: str) -> None:
        """ограничить число тяжёлых multipart запросов до записи на диск"""

        await self._check_limit(
            f"spam:media_upload:{board_slug}:{client_ip}",
            MEDIA_UPLOAD_MAX_ATTEMPTS,
            MEDIA_UPLOAD_WINDOW_SECONDS,
            "media upload rate limit exceeded",
        )

    async def _check_limit(self, key: str, limit: int, ttl: int, detail: str) -> None:
        """проверить и увеличить счётчик без гонки запросов"""

        # 0 в настройках значит, что владелец выключил конкретный лимит
        if limit == 0:
            return

        allowed = await self._redis_client.eval(
            CHECK_AND_INCREMENT_SCRIPT,
            1,
            key,
            ttl,
            limit,
        )
        if allowed != 1:
            raise HTTPException(status_code=429, detail=detail)
