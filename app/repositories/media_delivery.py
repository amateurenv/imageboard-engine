"""только безопасные запросы выдачи готовых производных media"""

from dataclasses import dataclass

from asyncpg import Pool, Record


@dataclass(frozen=True, slots=True)
class DeliverableMedia:
    """минимум данных для внутреннего redirect без раскрытия storage path клиенту"""

    id: int
    public_key: str
    mime_type: str
    display_path: str
    preview_path: str
    media_kind: str
    canonical_path: str | None
    canonical_mime_type: str | None
    board_type: str


DELIVERABLE_MEDIA_SQL = """
SELECT media.id, media.public_key, media.mime_type, media.display_path, media.preview_path,
       media.media_kind, media.canonical_path, media.canonical_mime_type,
       board.board_type
FROM media
JOIN posts AS post ON post.id=media.post_id
JOIN threads AS thread ON thread.id=post.thread_id
JOIN boards AS board ON board.id=thread.board_id
WHERE media.public_key=$1
  AND media.processing_state='ready'
  AND media.post_id IS NOT NULL
  AND (
      (media.media_kind='image' AND media.display_path IS NOT NULL
       AND media.preview_path IS NOT NULL)
      OR (media.media_kind IN ('audio', 'voice') AND media.canonical_path IS NOT NULL)
  )
  AND post.is_hidden=FALSE
  AND thread.is_hidden=FALSE
  AND board.is_active=TRUE
"""


def _deliverable(row: Record) -> DeliverableMedia:
    """собрать внутреннюю запись только из уже проверенной строки postgresql"""

    return DeliverableMedia(
        id=row["id"],
        public_key=row["public_key"],
        mime_type=row["mime_type"],
        display_path=row["display_path"],
        preview_path=row["preview_path"],
        media_kind=row["media_kind"],
        canonical_path=row["canonical_path"],
        canonical_mime_type=row["canonical_mime_type"],
        board_type=row["board_type"],
    )


class MediaDeliveryRepository:
    """проверять видимость media одним запросом к postgresql"""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def get_deliverable(self, public_key: str) -> DeliverableMedia | None:
        """вернуть только готовое и видимое attached media"""

        row = await self._pool.fetchrow(DELIVERABLE_MEDIA_SQL, public_key)
        return None if row is None else _deliverable(row)
