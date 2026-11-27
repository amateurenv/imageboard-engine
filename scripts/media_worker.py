"""воркер создания превью загруженных файлов из redis-очереди"""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from PIL import Image

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.repositories.media import MediaRepository

# redis будит worker раньше следующей проверки, но postgresql остаётся источником истины
QUEUE_KEY = "media:previews"
# после трёх ошибок нужна явная ручная повторная постановка
MAX_PROCESSING_ATTEMPTS = 3
# worker обязан завершить задачу за это время или следующий запуск её освободит
PROCESSING_LEASE = timedelta(minutes=10)
# даже без redis worker проверит очередь и вернёт зависшие задачи
IDLE_WAIT_SECONDS = 1
# превью лежат отдельно от оригиналов, поэтому их можно пересобрать
PREVIEW_DIR = Path("media/previews")
# thumbnail сохранит пропорции и впишет картинку в эти границы
PREVIEW_SIZE = (320, 180)
logger = logging.getLogger(__name__)


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
        # Pillow читает файл синхронно, выносим работу из event loop
        await asyncio.to_thread(make_preview, media.id, str(Path("media") / media.path))


async def run_worker() -> None:
    """бесконечный цикл обработки очереди"""

    settings = get_settings()

    redis_client = create_redis_client(settings)
    pool = await create_postgres_pool(settings)
    repository = MediaRepository(pool)

    try:
        while True:
            # lease освобождает задачу, если прошлый worker остановился во время обработки
            recovered = await repository.recover_expired(max_attempts=MAX_PROCESSING_ATTEMPTS)
            if recovered:
                logger.warning("recovered %s expired media job(s)", len(recovered))

            # FOR UPDATE SKIP LOCKED не даст двум worker захватить одну запись
            media = await repository.claim_next(
                max_attempts=MAX_PROCESSING_ATTEMPTS,
                lease_duration=PROCESSING_LEASE,
            )
            if media is None:
                # значение не является источником истины: оно только сокращает задержку пробуждения
                await redis_client.brpop(QUEUE_KEY, timeout=IDLE_WAIT_SECONDS)
                continue
            try:
                await process_job(repository, media.id)
            except Exception as error:
                result = await repository.fail_processing(
                    media.id,
                    max_attempts=MAX_PROCESSING_ATTEMPTS,
                    error=str(error),
                )
                if result is not None and result.processing_state.value == "queued":
                    # запись уже сохранена в postgresql, поэтому ошибка lpush не потеряет retry
                    await redis_client.lpush(QUEUE_KEY, media.id)
                logger.exception("media job %s failed", media.id)
            else:
                await repository.mark_ready(media.id)
    finally:
        await close_postgres_pool(pool)
        await close_redis_client(redis_client)


if __name__ == "__main__":
    asyncio.run(run_worker())
