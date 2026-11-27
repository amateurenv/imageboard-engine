"""sql запросы для файлов медиа"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from asyncpg import Connection, Pool, Record

from app.services.media_queue import (
    MediaProcessingState,
    ensure_media_transition,
)

CREATE_MEDIA_SQL = """
INSERT INTO media (
    path, size, mime_type, is_animation, board_id, posting_claim_id,
    media_kind, duration_seconds, channels, sample_rate
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
          , board_id, posting_claim_id, display_path, preview_path, is_animation,
            media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

GET_BY_ID_SQL = """
SELECT id, path, size, mime_type, created_at, processing_state,
       processing_attempts, processing_lease_until, last_processing_error,
       board_id, posting_claim_id, display_path, preview_path, is_animation,
       media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
FROM media
WHERE id = $1
"""

CLAIM_NEXT_MEDIA_SQL = """
WITH next_media AS (
    SELECT id
    FROM media
    WHERE processing_state = 'queued'
      AND processing_attempts < $1
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE media AS candidate
SET processing_state = 'processing',
    processing_attempts = candidate.processing_attempts + 1,
    processing_lease_until = clock_timestamp() + $2::interval
FROM next_media
WHERE candidate.id = next_media.id
RETURNING candidate.id, candidate.path, candidate.size, candidate.mime_type,
          candidate.created_at, candidate.processing_state,
          candidate.processing_attempts, candidate.processing_lease_until,
          candidate.last_processing_error, candidate.board_id, candidate.posting_claim_id,
          candidate.display_path, candidate.preview_path, candidate.is_animation,
          candidate.media_kind, candidate.duration_seconds, candidate.channels,
          candidate.sample_rate, candidate.canonical_path, candidate.canonical_mime_type
"""

CLAIM_MEDIA_SQL = """
UPDATE media
SET processing_state = 'processing',
    processing_attempts = processing_attempts + 1,
    processing_lease_until = clock_timestamp() + $3::interval
WHERE id = $1
  AND processing_state = 'queued'
  AND processing_attempts < $2
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error,
          board_id, posting_claim_id, display_path, preview_path, is_animation,
          media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

MARK_READY_SQL = """
UPDATE media
SET processing_state = 'ready',
    processing_lease_until = NULL,
    last_processing_error = NULL,
    display_path = $2,
    preview_path = $3
WHERE id = $1 AND processing_state = 'processing'
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
          , board_id, posting_claim_id, display_path, preview_path, is_animation,
            media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

MARK_AUDIO_READY_SQL = """
UPDATE media
SET processing_state = 'ready',
    processing_lease_until = NULL,
    last_processing_error = NULL,
    canonical_path = $2,
    canonical_mime_type = $3
WHERE id = $1 AND processing_state = 'processing'
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error,
          board_id, posting_claim_id, display_path, preview_path, is_animation,
          media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

MARK_VIDEO_READY_SQL = """
WITH ready AS (
    UPDATE media
    SET processing_state = 'ready',
        processing_lease_until = NULL,
        last_processing_error = NULL,
        canonical_path = $3,
        canonical_mime_type = 'video/mp4',
        preview_path = $4,
        duration_seconds = $5,
        channels = $6,
        sample_rate = $7
    WHERE id = $1 AND processing_state = 'processing'
      AND processing_lease_until = $2 AND processing_lease_until > clock_timestamp()
      AND media_kind IN ('video', 'video_note')
    RETURNING path
)
INSERT INTO media_cleanup_tasks (relative_path)
SELECT path FROM ready
ON CONFLICT (relative_path) DO UPDATE SET completed_at = NULL
RETURNING relative_path
"""

MARK_STICKER_READY_SQL = """
WITH ready AS (
    UPDATE media
    SET processing_state = 'ready',
        processing_lease_until = NULL,
        last_processing_error = NULL,
        display_path = $3
    WHERE id = $1 AND processing_state = 'processing' AND media_kind = 'sticker'
      AND processing_lease_until = $2 AND processing_lease_until > clock_timestamp()
    RETURNING path
)
INSERT INTO media_cleanup_tasks (relative_path)
SELECT path FROM ready
ON CONFLICT (relative_path) DO UPDATE SET completed_at = NULL
RETURNING relative_path
"""

FAIL_PROCESSING_SQL = """
UPDATE media
SET processing_state = CASE
        WHEN processing_attempts >= $2 THEN 'failed'
        ELSE 'queued'
    END,
    processing_lease_until = NULL,
    last_processing_error = $3
WHERE id = $1 AND processing_state = 'processing'
  AND ($4::timestamptz IS NULL OR processing_lease_until = $4)
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error,
          board_id, posting_claim_id, display_path, preview_path, is_animation,
          media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

RECOVER_EXPIRED_SQL = """
UPDATE media
SET processing_state = CASE
        WHEN processing_attempts >= $1 THEN 'failed'
        ELSE 'queued'
    END,
    processing_lease_until = NULL,
    last_processing_error = 'processing lease expired'
WHERE processing_state = 'processing'
  AND processing_lease_until <= clock_timestamp()
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error,
          board_id, posting_claim_id, display_path, preview_path, is_animation,
          media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

RETRY_FAILED_SQL = """
UPDATE media
SET processing_state = 'queued',
    processing_attempts = 0,
    processing_lease_until = NULL,
    last_processing_error = NULL
WHERE id = $1 AND processing_state = 'failed'
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error,
          board_id, posting_claim_id, display_path, preview_path, is_animation,
          media_kind, duration_seconds, channels, sample_rate, canonical_path, canonical_mime_type
"""

DELETE_MEDIA_SQL = """
DELETE FROM media WHERE id = $1
"""

CLAIM_CLEANUP_TASK_SQL = """
WITH next_task AS (
    SELECT id
    FROM media_cleanup_tasks
    WHERE completed_at IS NULL
      AND (lease_until IS NULL OR lease_until <= clock_timestamp())
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE media_cleanup_tasks AS task
SET attempts = task.attempts + 1,
    lease_until = clock_timestamp() + $1::interval
FROM next_task
WHERE task.id = next_task.id
RETURNING task.id, task.relative_path, task.attempts
"""

COMPLETE_CLEANUP_TASK_SQL = """
UPDATE media_cleanup_tasks
SET completed_at = clock_timestamp(), lease_until = NULL, last_error = NULL
WHERE id = $1 AND completed_at IS NULL
"""

FAIL_CLEANUP_TASK_SQL = """
UPDATE media_cleanup_tasks
SET lease_until = NULL, last_error = $2
WHERE id = $1 AND completed_at IS NULL
"""


@dataclass(frozen=True, slots=True)
class MediaRecord:
    """данные одного файла медиа из postgresql"""

    id: int
    path: str
    size: int
    mime_type: str
    created_at: datetime
    processing_state: MediaProcessingState
    processing_attempts: int
    processing_lease_until: datetime | None
    last_processing_error: str | None
    board_id: int | None = None
    posting_claim_id: int | None = None
    display_path: str | None = None
    preview_path: str | None = None
    is_animation: bool = False
    media_kind: str = "image"
    duration_seconds: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    canonical_path: str | None = None
    canonical_mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class MediaCleanupTask:
    """одна безопасная попытка удалить controlled relative path"""

    id: int
    relative_path: str
    attempts: int


def _to_media_record(row: Record) -> MediaRecord:
    """собрать типизированную запись из строки postgresql"""

    return MediaRecord(
        id=row["id"],
        path=row["path"],
        size=row["size"],
        mime_type=row["mime_type"],
        created_at=row["created_at"],
        processing_state=MediaProcessingState(row["processing_state"]),
        processing_attempts=row["processing_attempts"],
        processing_lease_until=row["processing_lease_until"],
        last_processing_error=row["last_processing_error"],
        board_id=row["board_id"],
        posting_claim_id=row["posting_claim_id"],
        display_path=row["display_path"],
        preview_path=row["preview_path"],
        is_animation=row["is_animation"],
        media_kind=row["media_kind"],
        duration_seconds=row["duration_seconds"],
        channels=row["channels"],
        sample_rate=row["sample_rate"],
        canonical_path=row["canonical_path"],
        canonical_mime_type=row["canonical_mime_type"],
    )


class MediaRepository:
    """доступ к файлам медиа"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._postgres_pool = postgres_pool

    async def create(
        self,
        *,
        path: str,
        size: int,
        mime_type: str,
        is_animation: bool = False,
        media_kind: str = "image",
        duration_seconds: int | None = None,
        channels: int | None = None,
        sample_rate: int | None = None,
        board_id: int | None = None,
        posting_claim_id: int | None = None,
    ) -> MediaRecord:
        """сохранить запись о загруженном файле"""

        row = await self._postgres_pool.fetchrow(
            CREATE_MEDIA_SQL,
            path,
            size,
            mime_type,
            is_animation,
            board_id,
            posting_claim_id,
            media_kind,
            duration_seconds,
            channels,
            sample_rate,
        )
        if row is None:
            # insert без ошибки всегда возвращает строку
            raise RuntimeError("media not created")

        return _to_media_record(row)

    async def create_in_transaction(
        self,
        connection: Connection,
        *,
        path: str,
        size: int,
        mime_type: str,
        is_animation: bool,
        board_id: int,
        posting_claim_id: int,
        media_kind: str = "image",
        duration_seconds: int | None = None,
        channels: int | None = None,
        sample_rate: int | None = None,
    ) -> MediaRecord:
        """создать queued media только рядом с уже заблокированным claim"""

        row = await connection.fetchrow(
            CREATE_MEDIA_SQL,
            path,
            size,
            mime_type,
            is_animation,
            board_id,
            posting_claim_id,
            media_kind,
            duration_seconds,
            channels,
            sample_rate,
        )
        if row is None:
            raise RuntimeError("media not created")
        return _to_media_record(row)

    async def get_by_id(self, media_id: int) -> MediaRecord | None:
        """вернуть файл по id, None если нет"""

        row = await self._postgres_pool.fetchrow(GET_BY_ID_SQL, media_id)
        if row is None:
            return None

        return _to_media_record(row)

    async def delete(self, media_id: int) -> None:
        """удалить запись, если очередь не приняла задание"""

        await self._postgres_pool.execute(DELETE_MEDIA_SQL, media_id)

    async def claim_next(
        self,
        *,
        max_attempts: int,
        lease_duration: timedelta,
    ) -> MediaRecord | None:
        """атомарно захватить одну ожидающую задачу без ожидания заблокированных"""

        ensure_media_transition(MediaProcessingState.QUEUED, MediaProcessingState.PROCESSING)
        row = await self._postgres_pool.fetchrow(
            CLAIM_NEXT_MEDIA_SQL, max_attempts, lease_duration
        )
        return None if row is None else _to_media_record(row)

    async def claim_by_id(
        self,
        media_id: int,
        *,
        max_attempts: int,
        lease_duration: timedelta,
    ) -> MediaRecord | None:
        """захватить конкретную задачу, если другой worker ещё не успел"""

        ensure_media_transition(MediaProcessingState.QUEUED, MediaProcessingState.PROCESSING)
        row = await self._postgres_pool.fetchrow(
            CLAIM_MEDIA_SQL, media_id, max_attempts, lease_duration
        )
        return None if row is None else _to_media_record(row)

    async def mark_ready(
        self,
        media_id: int,
        *,
        display_path: str | None = None,
        preview_path: str | None = None,
    ) -> MediaRecord | None:
        """завершить только задачу, которую worker уже захватил"""

        ensure_media_transition(MediaProcessingState.PROCESSING, MediaProcessingState.READY)
        row = await self._postgres_pool.fetchrow(
            MARK_READY_SQL, media_id, display_path, preview_path
        )
        return None if row is None else _to_media_record(row)

    async def mark_audio_ready(
        self, media_id: int, *, canonical_path: str, canonical_mime_type: str
    ) -> MediaRecord | None:
        """завершить audio только после записи canonical derivative"""

        ensure_media_transition(MediaProcessingState.PROCESSING, MediaProcessingState.READY)
        row = await self._postgres_pool.fetchrow(
            MARK_AUDIO_READY_SQL, media_id, canonical_path, canonical_mime_type
        )
        return None if row is None else _to_media_record(row)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """держать блокировку записи до завершения публикации производных файлов"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                yield connection

    async def mark_video_ready(
        self, connection: Connection, media_id: int, *, lease: datetime,
        canonical_path: str, preview_path: str, duration_seconds: int,
        channels: int | None, sample_rate: int | None,
    ) -> bool:
        """подтвердить текущий lease и запланировать очистку оригинала в одном commit"""

        return await connection.fetchval(
            MARK_VIDEO_READY_SQL, media_id, lease, canonical_path, preview_path,
            duration_seconds, channels, sample_rate,
        ) is not None

    async def mark_sticker_ready(
        self, connection: Connection, media_id: int, *, lease: datetime, display_path: str,
    ) -> bool:
        """запланировать очистку quarantine в той же транзакции, что и готовность стикера"""

        return await connection.fetchval(
            MARK_STICKER_READY_SQL, media_id, lease, display_path,
        ) is not None

    async def fail_processing(
        self,
        media_id: int,
        *,
        max_attempts: int,
        error: str,
        lease: datetime | None = None,
    ) -> MediaRecord | None:
        """записать ошибку и выбрать retry или окончательный failed"""

        error_text = error[:1_000]
        row = await self._postgres_pool.fetchrow(
            FAIL_PROCESSING_SQL, media_id, max_attempts, error_text, lease,
        )
        if row is None:
            return None
        record = _to_media_record(row)
        ensure_media_transition(MediaProcessingState.PROCESSING, record.processing_state)
        return record

    async def recover_expired(self, *, max_attempts: int) -> list[MediaRecord]:
        """освободить lease задач, оставленных упавшим worker"""

        rows = await self._postgres_pool.fetch(RECOVER_EXPIRED_SQL, max_attempts)
        records = [_to_media_record(row) for row in rows]
        for record in records:
            ensure_media_transition(MediaProcessingState.PROCESSING, record.processing_state)
        return records

    async def retry_failed(self, media_id: int) -> MediaRecord | None:
        """начать новый ручной цикл попыток только для failed задачи"""

        ensure_media_transition(MediaProcessingState.FAILED, MediaProcessingState.QUEUED)
        row = await self._postgres_pool.fetchrow(RETRY_FAILED_SQL, media_id)
        return None if row is None else _to_media_record(row)

    async def claim_cleanup_task(self, *, lease_duration: timedelta) -> MediaCleanupTask | None:
        """захватить один путь для очистки без параллельного удаления"""

        row = await self._postgres_pool.fetchrow(CLAIM_CLEANUP_TASK_SQL, lease_duration)
        if row is None:
            return None
        return MediaCleanupTask(row["id"], row["relative_path"], row["attempts"])

    async def complete_cleanup_task(self, task_id: int) -> None:
        """пометить задачу физической очистки завершённой"""

        await self._postgres_pool.execute(COMPLETE_CLEANUP_TASK_SQL, task_id)

    async def fail_cleanup_task(self, task_id: int, error: str) -> None:
        """сохранить ошибку и вернуть задачу следующему безопасному запуску"""

        await self._postgres_pool.execute(FAIL_CLEANUP_TASK_SQL, task_id, error[:1_000])
