"""worker безопасно перекодирует image из quarantine в закрытые производные файлы"""

import asyncio
import logging
import os
from datetime import timedelta

from PIL import Image, ImageOps

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.repositories.media import MediaRepository
from app.services.audio_processing import FFmpegMediaProcessor
from app.services.image_validation import validate_image
from app.services.media_scanner import MediaScanner, NoopMediaScanner
from app.services.media_storage import MediaStorage

# redis будит worker раньше следующей проверки, но postgresql остаётся источником истины
QUEUE_KEY = "media:previews"
# после трёх ошибок нужна явная ручная повторная постановка
MAX_PROCESSING_ATTEMPTS = 3
# worker обязан завершить задачу за это время или следующий запуск её освободит
PROCESSING_LEASE = timedelta(minutes=10)
# даже без redis worker проверит очередь и вернёт зависшие задачи
IDLE_WAIT_SECONDS = 1
# preview нужен спискам, display не даёт раздавать исходный пользовательский файл
PREVIEW_SIZE = (320, 180)
DISPLAY_SIZE = (2560, 2560)
logger = logging.getLogger(__name__)


def _save_webp(image: Image.Image, storage: MediaStorage, relative_path: str) -> None:
    """сохранить новый WebP через временный файл без metadata оригинала"""

    temporary_path = storage.temporary_path(relative_path)
    try:
        image.save(temporary_path, "WEBP", quality=85, method=6)
        os.chmod(temporary_path, 0o600)
        storage.replace_from_temporary(temporary_path, relative_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _normalized_static_frame(source: Image.Image) -> Image.Image:
    """скопировать один кадр без EXIF, ICC и palette metadata оригинала"""

    normalized = ImageOps.exif_transpose(source).copy()
    if normalized.mode not in {"RGB", "RGBA"}:
        return normalized.convert("RGBA" if "transparency" in source.info else "RGB")
    return normalized


def _save_animated_webp(
    source: Image.Image, storage: MediaStorage, relative_path: str
) -> None:
    """перекодировать animation по одному кадру без загрузки всех кадров в память"""

    temporary_path = storage.temporary_path(relative_path)
    try:
        frame_count = getattr(source, "n_frames", 1)
        durations: list[int] = []
        for frame_index in range(frame_count):
            source.seek(frame_index)
            duration = source.info.get("duration", 0)
            if not isinstance(duration, int) or duration < 0:
                raise ValueError("animation frame duration is invalid")
            durations.append(duration)
        source.seek(0)
        # Pillow's WebP encoder iterates source.n_frames itself, so append_images is not needed
        source.save(
            temporary_path,
            "WEBP",
            save_all=True,
            duration=durations,
            loop=source.info.get("loop", 0),
            quality=85,
            method=6,
        )
        os.chmod(temporary_path, 0o600)
        storage.replace_from_temporary(temporary_path, relative_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def make_derivatives(
    source_path: str, storage: MediaStorage, media_id: int, is_animation: bool = False
) -> tuple[str, str]:
    """создать server-side display и статичный preview без metadata оригинала"""

    display_path = f"display/{media_id}.webp"
    preview_path = f"previews/{media_id}.webp"
    try:
        with Image.open(source_path) as source:
            actual_animation = getattr(source, "n_frames", 1) > 1
            if actual_animation != is_animation:
                raise ValueError("validated animation state changed before processing")
            source.seek(0)
            # новый объект не переносит EXIF, ICC и произвольные поля из исходного файла
            preview = _normalized_static_frame(source)
            preview.thumbnail(PREVIEW_SIZE)
            _save_webp(preview, storage, preview_path)
            if actual_animation:
                _save_animated_webp(source, storage, display_path)
            else:
                display = _normalized_static_frame(source)
                display.thumbnail(DISPLAY_SIZE)
                _save_webp(display, storage, display_path)
    except Exception:
        storage.delete_file(display_path)
        storage.delete_file(preview_path)
        raise
    return display_path, preview_path


async def process_job(
    repository: MediaRepository,
    media_id: int,
    storage: MediaStorage,
    scanner: MediaScanner | None = None,
    audio_processor: FFmpegMediaProcessor | None = None,
) -> None:
    """создать производные файлы и пометить запись ready только в самом конце"""

    media = await repository.get_by_id(media_id)
    if media is None:
        return
    source_path = storage.resolve(media.path)
    # production scanner подключат здесь - до derivative и перевода media в ready
    await asyncio.to_thread((scanner or NoopMediaScanner()).scan, source_path)
    if media.media_kind in {"audio", "voice"}:
        processor = audio_processor or FFmpegMediaProcessor(
            get_settings().ffmpeg_bin, get_settings().ffprobe_bin
        )
        inspected = await asyncio.to_thread(processor.inspect, source_path)
        if inspected.media_type.value != media.media_kind:
            raise ValueError("media kind does not match its validated source")
        canonical_path: str | None = None
        try:
            canonical_path, canonical_mime_type = await asyncio.to_thread(
                processor.transcode, source_path, storage, media.id, inspected
            )
            # transcode возвращает путь только после atomic replace в storage
            assert canonical_path is not None
            storage.delete_file(media.path)
            ready = await repository.mark_audio_ready(
                media.id,
                canonical_path=canonical_path,
                canonical_mime_type=canonical_mime_type,
            )
            if ready is None:
                raise RuntimeError("media processing lease was lost")
        except Exception:
            if canonical_path is not None:
                storage.delete_file(canonical_path)
            raise
        return

    # проверка повторяется в worker, потому что очередь может пережить обновление процесса
    validated = await asyncio.to_thread(validate_image, source_path)
    if validated.is_animation != media.is_animation:
        raise ValueError("media animation state does not match its validated source")
    display_path, preview_path = await asyncio.to_thread(
        make_derivatives, str(source_path), storage, media.id, media.is_animation
    )
    try:
        # оригинал из quarantine не нужен после успешного server-side re-encode
        storage.delete_file(media.path)
        ready = await repository.mark_ready(
            media.id, display_path=display_path, preview_path=preview_path
        )
        if ready is None:
            raise RuntimeError("media processing lease was lost")
    except Exception:
        storage.delete_file(display_path)
        storage.delete_file(preview_path)
        raise


async def run_worker() -> None:
    """бесконечный цикл обработки закрытой postgresql очереди"""

    settings = get_settings()
    storage = MediaStorage(settings.media_storage_root)
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
                await process_job(repository, media.id, storage)
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
    finally:
        await close_postgres_pool(pool)
        await close_redis_client(redis_client)


if __name__ == "__main__":
    asyncio.run(run_worker())
