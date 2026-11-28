"""worker перекодирует media из quarantine в закрытые производные файлы"""

import asyncio
import logging
import os
from collections.abc import Buffer
from datetime import datetime, timedelta
from io import BufferedWriter, FileIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from app.core.config import get_settings
from app.core.database import close_postgres_pool, create_postgres_pool
from app.core.redis import close_redis_client, create_redis_client
from app.repositories.media import MediaRecord, MediaRepository
from app.services.audio_processing import FFmpegMediaProcessor
from app.services.image_validation import validate_image
from app.services.media_policy import MediaType
from app.services.media_queue import MediaProcessingState
from app.services.media_quota import (
    ANIMATION_BYTES,
    DISK_FULL,
    IMAGE_BYTES,
    PREVIEW_BYTES,
    MediaOutputTooLarge,
    MediaQuota,
    MediaStorageFull,
    existing_charge,
    work_budget,
    work_limits,
)
from app.services.media_scanner import MediaScanner, NoopMediaScanner
from app.services.media_storage import MediaStorage
from app.services.sticker_processing import transcode_sticker
from app.services.tgs_processing import transcode_tgs
from app.services.video_processing import VIDEO_KINDS, VideoProcessor
from app.services.video_sticker_processing import WEBM_STICKER_MIME, transcode_video_sticker

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


class BoundedOutput(BufferedWriter):
    """отклонить запись webp до превышения выделенных байтов"""

    def __init__(self, path: Path, limit: int) -> None:
        super().__init__(FileIO(path, "xb"))
        self.limit = limit

    def write(self, buffer: Buffer, /) -> int:
        if self.tell() + memoryview(buffer).nbytes > self.limit:
            raise MediaOutputTooLarge("WebP output exceeds its size limit")
        return super().write(buffer)


def _write_webp(image: Image.Image, target: Path, limit: int) -> None:
    with BoundedOutput(target, limit) as output:
        image.save(output, "WEBP", quality=85, method=6)


def _normalized_static_frame(source: Image.Image) -> Image.Image:
    """скопировать один кадр без EXIF, ICC и palette metadata оригинала"""

    normalized = ImageOps.exif_transpose(source).copy()
    if normalized.mode not in {"RGB", "RGBA"}:
        return normalized.convert("RGBA" if "transparency" in source.info else "RGB")
    return normalized


def _write_animated_webp(source: Image.Image, target: Path) -> None:
    """перекодировать animation по одному кадру без загрузки всех кадров в память"""

    with BoundedOutput(target, ANIMATION_BYTES) as output:
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
            output,
            "WEBP",
            save_all=True,
            duration=durations,
            loop=source.info.get("loop", 0),
            quality=85,
            method=6,
        )


def make_derivatives(
    source_path: Path, display_path: Path, preview_path: Path, is_animation: bool = False,
) -> None:
    """записать ограниченные display и preview во временные файлы задачи"""

    with Image.open(source_path) as source:
        actual_animation = getattr(source, "n_frames", 1) > 1
        if actual_animation != is_animation:
            raise ValueError("validated animation state changed before processing")
        source.seek(0)
        preview = _normalized_static_frame(source)
        preview.thumbnail(PREVIEW_SIZE)
        _write_webp(preview, preview_path, PREVIEW_BYTES)
        if actual_animation:
            _write_animated_webp(source, display_path)
        else:
            display = _normalized_static_frame(source)
            display.thumbnail(DISPLAY_SIZE)
            _write_webp(display, display_path, IMAGE_BYTES)


def _output_paths(media: MediaRecord) -> tuple[str, ...]:
    if media.media_kind in VIDEO_KINDS:
        return f"video/{media.id}.mp4", f"previews/{media.id}.webp"
    if media.media_kind == "audio":
        return (f"audio/{media.id}.m4a",)
    if media.media_kind == "voice":
        return (f"voice/{media.id}.ogg",)
    if media.media_kind == "sticker" and media.is_animation:
        extension = "webm" if media.mime_type == WEBM_STICKER_MIME else "tgs"
        return f"stickers/{media.id}.{extension}", f"previews/{media.id}.webp"
    if media.media_kind == "sticker":
        return (f"display/{media.id}.webp",)
    return f"display/{media.id}.webp", f"previews/{media.id}.webp"


async def _discard_attempt(quota: MediaQuota, path: str, media_id: int) -> None:
    """убрать частичный результат до повтора, сохраняя успешный commit"""

    async with quota.transaction() as connection:
        state = await connection.fetchval(
            "SELECT processing_state FROM media WHERE id=$1 FOR UPDATE", media_id,
        )
        if state not in {"queued", "processing", "failed"}:
            return
        paths = await connection.fetchval(
            "SELECT work_paths FROM media_disk_files WHERE path=$1", path,
        )
        if paths:
            for temporary in paths:
                quota.storage.delete_file(temporary)
            await connection.execute(
                "UPDATE media_disk_files SET work_paths='{}' WHERE path=$1", path,
            )


async def _run_job(
    repository: MediaRepository, media: MediaRecord, lease: datetime, quota: MediaQuota,
    scanner: MediaScanner, audio_processor: FFmpegMediaProcessor, video_processor: VideoProcessor,
) -> None:
    storage = quota.storage
    await _discard_attempt(quota, media.path, media.id)
    finals = _output_paths(media)
    attempt = uuid4().hex
    temporary = tuple(storage.work_path(path, attempt) for path in finals)
    relative_temporary = [path.relative_to(storage.root).as_posix() for path in temporary]
    limits = work_limits(media.media_kind, media.mime_type, media.is_animation)
    work = work_budget(media.media_kind, media.mime_type, media.is_animation)
    source = storage.resolve(media.path)
    async with repository.transaction() as connection:
        if not await repository.lock_processing(connection, media.id, lease):
            raise RuntimeError("media processing lease was lost")
        if any(os.path.lexists(storage.resolve(path)) for path in finals):
            raise ValueError("unexpected file occupies a media derivative path")
        held = await connection.fetchval(
            "SELECT work_bytes FROM media_disk_files WHERE path=$1 FOR UPDATE", media.path,
        )
        if held is None:
            raise MediaStorageFull("media source is missing from inventory")
        if held < work:
            await quota.admit(connection, work - held)
        await quota.start_work(connection, media.path, work, [*relative_temporary, *finals])

    try:
        storage.prepare_parent(relative_temporary[0])
        await asyncio.to_thread(scanner.scan, source)
        duration = None
        canonical_mime = None
        video = None
        settings = quota.settings
        if media.media_kind in VIDEO_KINDS:
            video = await asyncio.to_thread(
                video_processor.transcode, source, temporary[0], temporary[1],
                MediaType(media.media_kind),
            )
        elif media.media_kind == "sticker":
            if media.mime_type == WEBM_STICKER_MIME:
                duration = await asyncio.to_thread(
                    transcode_video_sticker, source, temporary[0], temporary[1],
                    ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin,
                )
            elif media.is_animation:
                duration = await asyncio.to_thread(
                    transcode_tgs, source, temporary[0], temporary[1],
                )
            else:
                await asyncio.to_thread(transcode_sticker, source, temporary[0])
        elif media.media_kind in {"audio", "voice"}:
            inspected = await asyncio.to_thread(audio_processor.inspect, source)
            if inspected.media_type.value != media.media_kind:
                raise ValueError("media kind does not match its validated source")
            _, canonical_mime = await asyncio.to_thread(
                audio_processor.transcode, source, storage, media.id, inspected,
                target=temporary[0],
            )
        else:
            validated = await asyncio.to_thread(validate_image, source)
            if validated.is_animation != media.is_animation:
                raise ValueError("media animation state does not match its validated source")
            await asyncio.to_thread(
                make_derivatives, source, temporary[0], temporary[1], media.is_animation,
            )
        charges = []
        for path, limit in zip(temporary, limits, strict=True):
            if not 0 < path.stat().st_size <= limit:
                raise MediaOutputTooLarge("invalid derivative size")
            await asyncio.to_thread(storage.sync_file, path)
            charges.append(existing_charge(path))
        if sum(charges) > work:
            raise MediaOutputTooLarge("encoded files exceeded reserved blocks")
        async with repository.transaction() as connection:
            if not await repository.lock_processing(connection, media.id, lease):
                raise RuntimeError("media processing lease was lost")
            if video is not None:
                updated = await repository.mark_video_ready(
                    connection, media.id, lease=lease, canonical_path=finals[0],
                    preview_path=finals[1], duration_seconds=video.duration_seconds,
                    channels=video.channels, sample_rate=video.sample_rate,
                )
            elif media.media_kind == "sticker":
                updated = await repository.mark_sticker_ready(
                    connection, media.id, lease=lease, duration_seconds=duration,
                    display_path=None if media.is_animation else finals[0],
                    preview_path=finals[1] if media.is_animation else None,
                    canonical_path=finals[0] if media.is_animation else None,
                )
            else:
                await repository.mark_image_audio_ready(
                    connection, media.id,
                    display_path=finals[0] if canonical_mime is None else None,
                    preview_path=finals[1] if len(finals) == 2 else None,
                    canonical_path=finals[0] if canonical_mime else None,
                    canonical_mime_type=canonical_mime,
                )
                updated = True
            if not updated:
                raise RuntimeError("media processing lease was lost")
            for path, final, charge in zip(temporary, finals, charges, strict=True):
                storage.replace_from_temporary(path, final)
                await connection.execute(
                    "INSERT INTO media_disk_files(path, owner_path, media_id, bytes) "
                    "VALUES ($1, $2, $3, $4)", final, media.path, media.id, charge,
                )
            await connection.execute(
                "UPDATE media_disk_files SET work_bytes=0, work_paths='{}' WHERE path=$1",
                media.path,
            )
    except Exception:
        try:
            await _discard_attempt(quota, media.path, media.id)
        except Exception:
            logger.exception("could not clean a partial derivative for media %s", media.id)
        raise


async def process_job(
    repository: MediaRepository,
    media_id: int,
    storage: MediaStorage,
    scanner: MediaScanner | None = None,
    audio_processor: FFmpegMediaProcessor | None = None,
    *,
    expected_lease: datetime | None = None,
    video_processor: VideoProcessor | None = None,
) -> None:
    """удержать блокировку записи до полного завершения encoder"""

    media = await repository.get_by_id(media_id)
    if media is None or media.processing_state is not MediaProcessingState.PROCESSING:
        return
    lease = media.processing_lease_until
    if lease is None or (expected_lease is not None and lease != expected_lease):
        return
    quota = MediaQuota(repository.pool, storage)
    quota.verify_mount()
    with storage.guard(media.path, writer=True) as acquired:
        if not acquired:
            raise MediaStorageFull("previous media writer is still active")
        await quota.check_inventory()
        settings = quota.settings
        conversion = asyncio.create_task(_run_job(
            repository, media, lease, quota, scanner or NoopMediaScanner(),
            audio_processor or FFmpegMediaProcessor(settings.ffmpeg_bin, settings.ffprobe_bin),
            video_processor or VideoProcessor(settings.ffmpeg_bin, settings.ffprobe_bin),
        ))
        try:
            await asyncio.shield(conversion)
        except asyncio.CancelledError:
            await asyncio.gather(conversion, return_exceptions=True)
            raise


async def run_worker() -> None:
    """бесконечный цикл обработки закрытой postgresql очереди"""

    settings = get_settings()
    storage = MediaStorage(settings.media_storage_root)
    redis_client = create_redis_client(settings)
    pool = await create_postgres_pool(settings)
    repository = MediaRepository(pool)

    try:
        await MediaQuota(pool, storage, settings).check_inventory()
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
                try:
                    await redis_client.brpop(QUEUE_KEY, timeout=IDLE_WAIT_SECONDS)
                except Exception:
                    await asyncio.sleep(IDLE_WAIT_SECONDS)
                continue
            try:
                await process_job(
                    repository, media.id, storage, expected_lease=media.processing_lease_until,
                )
            except Exception as error:
                if isinstance(error, MediaStorageFull) or (
                    isinstance(error, OSError) and error.errno in DISK_FULL
                ):
                    await repository.defer_processing(
                        media.id, media.processing_lease_until, "media_storage_full",
                    )
                    continue
                await repository.fail_processing(
                    media.id,
                    max_attempts=MAX_PROCESSING_ATTEMPTS,
                    error=str(error),
                    lease=media.processing_lease_until,
                )
                logger.exception("media job %s failed", media.id)
    finally:
        await close_postgres_pool(pool)
        await close_redis_client(redis_client)


if __name__ == "__main__":
    asyncio.run(run_worker())
