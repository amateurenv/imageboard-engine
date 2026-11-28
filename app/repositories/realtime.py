"""видимость областей realtime и чтение транзакционной очереди"""

from asyncpg import Connection, Pool, Record

from app.schemas.realtime import RealtimeEvent


class RealtimeRepository:
    """читать области по тем же условиям, что и публичный rest"""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def get_board_id(self, slug: str) -> int | None:
        """разрешить активную доску, в том числе публично читаемый профиль"""

        return await self._pool.fetchval(
            "SELECT id FROM boards WHERE slug = $1 AND is_active", slug
        )

    async def get_thread_board_id(self, thread_id: int) -> int | None:
        """вернуть доску видимого треда"""

        return await self._pool.fetchval(
            """
            SELECT b.id FROM threads AS t JOIN boards AS b ON b.id = t.board_id
            WHERE t.id = $1 AND NOT t.is_hidden AND b.is_active
            """,
            thread_id,
        )


async def lock_pending_events(connection: Connection, limit: int) -> list[Record]:
    """временно закрепить пачку за текущим процессом"""

    return await connection.fetch(
        """
        SELECT id, board_id, thread_id, unavailable, created_at FROM realtime_outbox
        ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED
        """,
        limit,
    )


def read_event(row: Record) -> RealtimeEvent:
    """собрать уведомление из строго определённых полей очереди"""

    return RealtimeEvent(
        board_id=row["board_id"], thread_id=row["thread_id"], unavailable=row["unavailable"]
    )


async def delete_published_events(connection: Connection, event_ids: list[int]) -> None:
    """снять опубликованные записи с очереди"""

    await connection.execute("DELETE FROM realtime_outbox WHERE id = ANY($1::bigint[])", event_ids)
