"""sql запросы для файлов медиа"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool

CREATE_MEDIA_SQL = """
INSERT INTO media (path, size, mime_type)
VALUES ($1, $2, $3)
RETURNING id, path, size, mime_type, created_at
"""

GET_BY_ID_SQL = """
SELECT id, path, size, mime_type, created_at
FROM media
WHERE id = $1
"""


@dataclass(frozen=True, slots=True)
class MediaRecord:
    """данные одного файла медиа из postgresql"""

    id: int
    path: str
    size: int
    mime_type: str
    created_at: datetime


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

        return MediaRecord(
            id=row["id"],
            path=row["path"],
            size=row["size"],
            mime_type=row["mime_type"],
            created_at=row["created_at"],
        )

    async def get_by_id(self, media_id: int) -> MediaRecord | None:
        """вернуть файл по id, None если нет"""

        row = await self._postgres_pool.fetchrow(GET_BY_ID_SQL, media_id)
        if row is None:
            return None

        return MediaRecord(
            id=row["id"],
            path=row["path"],
            size=row["size"],
            mime_type=row["mime_type"],
            created_at=row["created_at"],
        )
