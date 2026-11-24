"""воркер создания превью загруженных файлов из redis-очереди"""

import asyncio
from pathlib import Path

from PIL import Image

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.repositories.media import MediaRepository

# ключ очереди с id медиафайлов
QUEUE_KEY = "media:previews"
# каталог превью
PREVIEW_DIR = Path("media/previews")
# максимальный размер превью
PREVIEW_SIZE = (320, 180)


def make_preview(media_id: int, path: str) -> None:
    """создать превью картинки на диск"""

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    with Image.open(path) as image:
        image.thumbnail(PREVIEW_SIZE)
        image.save(PREVIEW_DIR / f"{media_id}.jpg", "JPEG", quality=85)


async def process_job(repository: MediaRepository, media_id: int) -> None:
    """обработать задание: создать превью одного файла"""

    media = await repository.get_by_id(media_id)
    if media is None:
        return

    # превью для картинок; видео через ffmpeg — позже (Stage 7+)
    if media.mime_type.startswith("image/"):
        make_preview(media.id, media.path)


async def run_worker() -> None:
    """бесконечный цикл обработки очереди"""

    settings = get_settings()

    redis_client = create_redis_client(settings)
    pool = await create_postgres_pool(settings)
    repository = MediaRepository(pool)

    try:
        while True:
            _key, payload = await redis_client.brpop(QUEUE_KEY)
            await process_job(repository, int(payload))
    finally:
        await close_postgres_pool(pool)
        await close_redis_client(redis_client)


if __name__ == "__main__":
    asyncio.run(run_worker())
