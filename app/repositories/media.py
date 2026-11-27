"""sql запросы для файлов медиа"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from asyncpg import Pool, Record

from app.services.media_queue import (
    MediaProcessingState,
    ensure_media_transition,
)

CREATE_MEDIA_SQL = """
INSERT INTO media (path, size, mime_type)
VALUES ($1, $2, $3)
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
"""

GET_BY_ID_SQL = """
SELECT id, path, size, mime_type, created_at, processing_state,
       processing_attempts, processing_lease_until, last_processing_error
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
          candidate.last_processing_error
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
          processing_attempts, processing_lease_until, last_processing_error
"""

MARK_READY_SQL = """
UPDATE media
SET processing_state = 'ready',
    processing_lease_until = NULL,
    last_processing_error = NULL
WHERE id = $1 AND processing_state = 'processing'
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
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
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
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
          processing_attempts, processing_lease_until, last_processing_error
"""

RETRY_FAILED_SQL = """
UPDATE media
SET processing_state = 'queued',
    processing_attempts = 0,
    processing_lease_until = NULL,
    last_processing_error = NULL
WHERE id = $1 AND processing_state = 'failed'
RETURNING id, path, size, mime_type, created_at, processing_state,
          processing_attempts, processing_lease_until, last_processing_error
"""

DELETE_MEDIA_SQL = """
DELETE FROM media WHERE id = $1
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
    )


class MediaRepository:
    """доступ к файлам медиа"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._postgres_pool = postgres_pool

    async def create(self, *, path: str, size: int, mime_type: str) -> MediaRecord:
        """сохранить запись о загруженном файле"""

        row = await self._postgres_pool.fetchrow(CREATE_MEDIA_SQL, path, size, mime_type)
        if row is None:
            # insert без ошибки всегда возвращает строку
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

    async def mark_ready(self, media_id: int) -> MediaRecord | None:
        """завершить только задачу, которую worker уже захватил"""

        ensure_media_transition(MediaProcessingState.PROCESSING, MediaProcessingState.READY)
        row = await self._postgres_pool.fetchrow(MARK_READY_SQL, media_id)
        return None if row is None else _to_media_record(row)

    async def fail_processing(
        self,
        media_id: int,
        *,
        max_attempts: int,
        error: str,
    ) -> MediaRecord | None:
        """записать ошибку и выбрать retry или окончательный failed"""

        error_text = error[:1_000]
        row = await self._postgres_pool.fetchrow(
            FAIL_PROCESSING_SQL, media_id, max_attempts, error_text
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
