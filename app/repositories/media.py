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
      AND (processing_next_attempt_at IS NULL OR processing_next_attempt_at <= clock_timestamp())
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE media AS candidate
SET processing_state = 'processing',
    processing_attempts = candidate.processing_attempts + 1,
    processing_lease_until = clock_timestamp() + $2::interval,
    processing_next_attempt_at = NULL
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
  AND (processing_next_attempt_at IS NULL OR processing_next_attempt_at <= clock_timestamp())
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
        display_path = $3,
        preview_path = $4,
        canonical_path = $5,
        canonical_mime_type = CASE WHEN is_animation THEN mime_type END,
        duration_seconds = $6
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
    processing_next_attempt_at = NULL,
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
      AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE media_cleanup_tasks AS task
SET attempts = task.attempts + 1,
    lease_until = clock_timestamp() + $1::interval
FROM next_task
WHERE task.id = next_task.id
RETURNING task.id, task.relative_path, task.attempts, task.lease_until,
          coalesce(task.quota_owner_path, (SELECT owner_path FROM media_disk_files
                                          WHERE path = task.relative_path), task.relative_path)
          AS owner_path
"""

FAIL_CLEANUP_TASK_SQL = """
UPDATE media_cleanup_tasks
SET lease_until = NULL, last_error = $3,
    next_attempt_at = clock_timestamp() + interval '1 minute'
WHERE id = $1 AND lease_until = $2 AND completed_at IS NULL
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
    lease_until: datetime
    owner_path: str


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


async def queue_metrics(connection: Connection, backlog_age: timedelta) -> dict[str, int]:
    """посчитать необработанные задачи и ошибки в текущем снимке бд"""
    row = await connection.fetchrow(
        """SELECT processing.*, cleanup.* FROM (
            SELECT count(*) FILTER (WHERE processing_state='queued') AS queued,
                   count(*) FILTER (WHERE processing_state='processing') AS processing,
                   count(*) FILTER (WHERE processing_state='failed') AS failed,
                   count(*) FILTER (WHERE processing_state='processing'
                       AND processing_lease_until <= now()) AS expired_processing_leases,
                   count(*) FILTER (WHERE processing_state='queued'
                       AND created_at <= now() - $1::interval) AS old_queued,
                   count(*) FILTER (WHERE processing_state='queued'
                       AND last_processing_error IS NOT NULL) AS processing_retry_errors,
                   count(*) FILTER (WHERE processing_state='queued'
                       AND last_processing_error='media_storage_full') AS storage_deferrals
            FROM media WHERE processing_state IN ('queued', 'processing', 'failed')
        ) AS processing CROSS JOIN (
            SELECT count(*) AS cleanup_pending,
                   count(*) FILTER (WHERE created_at <= now() - $1::interval) AS old_cleanup,
                   count(*) FILTER (WHERE lease_until <= now()) AS expired_cleanup_leases,
                   count(*) FILTER (WHERE last_error IS NOT NULL) AS cleanup_errors
            FROM media_cleanup_tasks WHERE completed_at IS NULL
        ) AS cleanup""",
        backlog_age,
    )
    assert row is not None
    return dict(row)


class MediaRepository:
    """доступ к файлам медиа"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._postgres_pool = postgres_pool

    @property
    def pool(self) -> Pool:
        """использовать тот же пул для транзакций учёта"""

        return self._postgres_pool

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


    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """держать блокировку записи до завершения публикации производных файлов"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                yield connection

    async def lock_processing(self, connection: Connection, media_id: int, lease: datetime) -> bool:
        """проверить срок и удержать строку до публикации файлов"""

        return bool(await connection.fetchval(
            """SELECT true FROM media WHERE id=$1 AND processing_state='processing'
                 AND processing_lease_until=$2 AND processing_lease_until > clock_timestamp()
               FOR UPDATE""",
            media_id, lease,
        ))

    async def mark_image_audio_ready(
        self, connection: Connection, media_id: int, *, display_path: str | None,
        preview_path: str | None, canonical_path: str | None, canonical_mime_type: str | None,
    ) -> None:
        """сохранить производные и очередь очистки в общей транзакции"""

        await connection.execute(
            """WITH ready AS (
                   UPDATE media SET processing_state='ready', processing_lease_until=NULL,
                       last_processing_error=NULL, display_path=$2, preview_path=$3,
                       canonical_path=$4, canonical_mime_type=$5
                   WHERE id=$1 RETURNING path
               )
               INSERT INTO media_cleanup_tasks (relative_path)
               SELECT path FROM ready ON CONFLICT (relative_path) DO NOTHING""",
            media_id, display_path, preview_path, canonical_path, canonical_mime_type,
        )

    async def defer_processing(self, media_id: int, lease: datetime | None, reason: str) -> None:
        """отложить временную нехватку ресурсов без расходования попытки"""

        await self._postgres_pool.execute(
            """UPDATE media SET processing_state='queued', processing_lease_until=NULL,
                   processing_next_attempt_at=clock_timestamp() + interval '15 seconds',
                   processing_attempts=greatest(processing_attempts - 1, 0),
                   last_processing_error=$3
               WHERE id=$1 AND processing_state='processing' AND processing_lease_until=$2""",
            media_id, lease, reason,
        )

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
        self, connection: Connection, media_id: int, *, lease: datetime,
        display_path: str | None, preview_path: str | None,
        canonical_path: str | None, duration_seconds: int | None,
    ) -> bool:
        """запланировать очистку quarantine в той же транзакции, что и готовность стикера"""

        return await connection.fetchval(
            MARK_STICKER_READY_SQL, media_id, lease, display_path, preview_path,
            canonical_path, duration_seconds,
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
        return MediaCleanupTask(
            row["id"], row["relative_path"], row["attempts"], row["lease_until"], row["owner_path"],
        )

    async def complete_cleanup_task(self, task: MediaCleanupTask) -> None:
        """пометить задачу физической очистки завершённой"""

        async with self.transaction() as connection:
            completed = await connection.fetchval(
                """UPDATE media_cleanup_tasks SET completed_at=clock_timestamp(),
                       lease_until=NULL, last_error=NULL, next_attempt_at=NULL
                   WHERE id=$1 AND lease_until=$2 AND completed_at IS NULL RETURNING true""",
                task.id, task.lease_until,
            )
            if not completed:
                return
            await connection.execute(
                """UPDATE media_disk_files
                   SET work_paths=array_remove(work_paths, $1)
                   WHERE path=$2 AND $1=ANY(work_paths)""",
                task.relative_path, task.owner_path,
            )
            await connection.execute(
                """UPDATE media_disk_files SET bytes=0 WHERE path=$1""", task.relative_path,
            )
            await connection.execute(
                """DELETE FROM media_disk_files AS disk WHERE path IN ($1, $2)
                     AND disk.bytes=0 AND cardinality(disk.work_paths)=0
                     AND (disk.path=$1 OR NOT EXISTS (
                         SELECT 1 FROM media WHERE id=disk.media_id
                     ))""",
                task.relative_path, task.owner_path,
            )
            await connection.execute(
                """UPDATE media_disk_files AS disk SET work_bytes=0 FROM media
                   WHERE disk.path=$1 AND disk.media_id=media.id
                     AND media.processing_state='failed' AND cardinality(disk.work_paths)=0""",
                task.owner_path,
            )

    async def fail_cleanup_task(self, task: MediaCleanupTask, error: str) -> None:
        """сохранить ошибку и вернуть задачу следующему безопасному запуску"""

        await self._postgres_pool.execute(
            FAIL_CLEANUP_TASK_SQL, task.id, task.lease_until, error[:1_000],
        )

    async def queue_expired_uploads(self) -> None:
        """добавить записи для карантина, оставленного процессом upload"""

        await self._postgres_pool.execute(
            """INSERT INTO media_cleanup_tasks (relative_path, quota_owner_path)
               SELECT path, owner_path FROM media_disk_files
               WHERE expires_at <= clock_timestamp()
               ON CONFLICT (relative_path) DO NOTHING"""
        )

    async def queue_failed_derivatives(self) -> None:
        """убрать временные файлы терминальных задач и вернуть неиспользуемый резерв"""

        async with self.transaction() as connection:
            await connection.execute(
                """INSERT INTO media_cleanup_tasks (relative_path, quota_owner_path)
                   SELECT unnest(disk.work_paths), disk.owner_path FROM media_disk_files disk
                   JOIN media ON media.id=disk.media_id AND media.processing_state='failed'
                   ON CONFLICT (relative_path) DO UPDATE SET
                       quota_owner_path=EXCLUDED.quota_owner_path,
                       completed_at=NULL, lease_until=NULL, next_attempt_at=NULL, last_error=NULL
                   WHERE media_cleanup_tasks.completed_at IS NOT NULL"""
            )
            await connection.execute(
                """UPDATE media_disk_files disk SET work_bytes=0 FROM media
                   WHERE media.id=disk.media_id AND media.processing_state='failed'
                     AND disk.work_bytes>0 AND cardinality(disk.work_paths)=0"""
            )

    async def cleanup_still_allowed(self, task: MediaCleanupTask) -> bool:
        """проверить захват очистки и убедиться, что путь больше не нужен media"""

        return bool(await self._postgres_pool.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM media_cleanup_tasks
                   WHERE id=$2 AND lease_until=$3 AND completed_at IS NULL
               ) AND NOT EXISTS (
                   SELECT 1 FROM media_disk_files AS disk LEFT JOIN media ON media.id=disk.media_id
                   WHERE (disk.path=$1 AND (NOT disk.managed
                         OR (media.id IS NOT NULL
                             AND (media.path<>disk.path OR media.processing_state<>'ready'))))
                      OR ($1=ANY(disk.work_paths)
                          AND media.processing_state IN ('queued', 'processing'))
               )""",
            task.relative_path, task.id, task.lease_until,
        ))

    async def cancel_cleanup_task(self, task: MediaCleanupTask) -> None:
        """отменить просроченную задачу, путь которой теперь принадлежит media"""

        await self._postgres_pool.execute(
            """DELETE FROM media_cleanup_tasks WHERE id=$1 AND lease_until=$2""",
            task.id, task.lease_until,
        )
