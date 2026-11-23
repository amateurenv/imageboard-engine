"""создание и закрытие асинхронного клиента redis"""

# redis python пакет, его asyncio модуль не блокирует event loop fastapi
from redis.asyncio import Redis

# Settings содержит проверенный REDIS_URL из .env
from app.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """создать клиент redis без сетевого запроса"""

    return Redis.from_url(
        # адрес из .env, не из кода
        str(settings.redis_url),

        # текст возвращается как str, не bytes
        decode_responses=True,
    )


async def close_redis_client(redis_client: Redis) -> None:
    """закрыть соединения redis при остановке"""

    # aclose закрывает клиент и его соединения
    await redis_client.aclose()
