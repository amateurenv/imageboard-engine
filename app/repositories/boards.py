"""sql запросы для досок"""

import json
from dataclasses import dataclass
from datetime import timedelta

from asyncpg import Connection, Pool, Record

# один запрос даёт список досок и согласованные счётчики на тот же момент
# $1 - timedelta, он приходит параметром, а не склеивается с sql
LIST_ACTIVE_BOARDS_SQL = """
SELECT
    b.slug,
    b.title,
    b.description,
    b.is_nsfw,
    count(DISTINCT t.id) FILTER (WHERE t.is_archived = FALSE)::integer AS thread_count,
    count(p.id)::integer AS post_count,
    count(p.id) FILTER (WHERE p.created_at >= now() - $1::interval)::integer AS active_post_count
FROM boards AS b
LEFT JOIN threads AS t ON t.board_id = b.id
LEFT JOIN posts AS p ON p.thread_id = t.id
WHERE b.is_active = TRUE AND b.board_type = 'public'
GROUP BY b.id
ORDER BY b.position ASC, b.id ASC
"""

GET_ACTIVE_BOARD_SQL = """
SELECT
    b.slug,
    b.title,
    b.description,
    b.is_nsfw,
    count(DISTINCT t.id) FILTER (WHERE t.is_archived = FALSE)::integer AS thread_count,
    count(p.id)::integer AS post_count,
    count(p.id) FILTER (WHERE p.created_at >= now() - $2::interval)::integer AS active_post_count
FROM boards AS b
LEFT JOIN threads AS t ON t.board_id = b.id
LEFT JOIN posts AS p ON p.thread_id = t.id
WHERE b.is_active = TRUE AND b.board_type = 'public' AND b.slug = $1
GROUP BY b.id
"""

# поля доски для полного представления в админке
BOARD_COLUMNS = "slug, title, description, position, is_active, is_nsfw, media_overrides"

SELECT_BY_SLUG_SQL = f"""
SELECT {BOARD_COLUMNS}
FROM boards
WHERE slug = $1
"""

CREATE_BOARD_SQL = f"""
INSERT INTO boards (slug, title, description, position, is_active, is_nsfw, media_overrides)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
RETURNING {BOARD_COLUMNS}
"""

LIST_ALL_BOARDS_SQL = """
SELECT id, slug, title, description, position, is_active, is_nsfw, media_overrides
FROM boards
ORDER BY position ASC, id ASC
"""

DELETE_BOARD_SQL = """
DELETE FROM boards WHERE slug = $1
"""

DELETE_BOARD_WITH_THREADS_SQL = """
WITH target AS (
    SELECT id
    FROM boards
    WHERE slug = $1
    FOR UPDATE
), deleted_threads AS (
    DELETE FROM threads
    WHERE board_id = (SELECT id FROM target)
)
DELETE FROM boards
WHERE id = (SELECT id FROM target)
RETURNING id
"""

UPDATE_BOARD_SQL = f"""
UPDATE boards
SET title = $2, description = $3, position = $4, is_active = $5, is_nsfw = $6,
    media_overrides = $7::jsonb
WHERE slug = $1
RETURNING {BOARD_COLUMNS}
"""


@dataclass(frozen=True, slots=True)
class BoardRecord:
    """данные одной доски, полученные из postgresql"""

    slug: str
    title: str
    description: str
    is_nsfw: bool
    thread_count: int
    post_count: int
    active_post_count: int


@dataclass(frozen=True, slots=True)
class BoardDetailRecord:
    """полные данные доски, доступные админке"""

    slug: str
    title: str
    description: str
    position: int
    is_active: bool
    is_nsfw: bool
    media_overrides: dict[str, object]


def _detail_record(row: Record) -> BoardDetailRecord:
    """превратить строку базы в запись доски"""

    raw_overrides = row["media_overrides"]
    # asyncpg обычно отдаёт jsonb строкой, а тестовый driver может отдать уже dict
    media_overrides = (
        raw_overrides if isinstance(raw_overrides, dict) else json.loads(raw_overrides)
    )

    return BoardDetailRecord(
        slug=row["slug"],
        title=row["title"],
        description=row["description"],
        position=row["position"],
        is_active=row["is_active"],
        is_nsfw=row["is_nsfw"],
        media_overrides=media_overrides,
    )


class BoardsRepository:
    """чтение и управление досками в postgresql"""

    def __init__(self, postgres_pool: Pool) -> None:
        # сохраняем общий пул, а не открываем новое подключение
        self._postgres_pool = postgres_pool

    async def list_active_boards(self, activity_window_hours: int) -> list[BoardRecord]:
        """вернуть активные доски в порядке отображения"""

        # timedelta передаём параметром: даже owner не может подставить в sql свой текст
        rows = await self._postgres_pool.fetch(
            LIST_ACTIVE_BOARDS_SQL, timedelta(hours=activity_window_hours)
        )

        return [
            BoardRecord(
                slug=row["slug"],
                title=row["title"],
                description=row["description"],
                is_nsfw=row["is_nsfw"],
                thread_count=row["thread_count"],
                post_count=row["post_count"],
                active_post_count=row["active_post_count"],
            )
            for row in rows
        ]

    async def get_by_slug(self, slug: str) -> BoardDetailRecord | None:
        """вернуть доску по slug"""

        row = await self._postgres_pool.fetchrow(SELECT_BY_SLUG_SQL, slug)

        if row is None:
            return None

        return _detail_record(row)

    async def get_active_board(
        self, slug: str, activity_window_hours: int
    ) -> BoardRecord | None:
        """вернуть публичную доску с точными счётчиками"""

        row = await self._postgres_pool.fetchrow(
            GET_ACTIVE_BOARD_SQL, slug, timedelta(hours=activity_window_hours)
        )
        if row is None:
            return None
        return BoardRecord(
            slug=row["slug"],
            title=row["title"],
            description=row["description"],
            is_nsfw=row["is_nsfw"],
            thread_count=row["thread_count"],
            post_count=row["post_count"],
            active_post_count=row["active_post_count"],
        )

    async def get_by_slug_for_update(
        self, connection: Connection, slug: str
    ) -> BoardDetailRecord | None:
        """прочитать доску с блокировкой для частичного изменения"""

        row = await connection.fetchrow(f"{SELECT_BY_SLUG_SQL} FOR UPDATE", slug)
        return None if row is None else _detail_record(row)

    async def create_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
        media_overrides: dict[str, object],
    ) -> BoardDetailRecord:
        """создать доску и вернуть запись с полными данными"""

        row = await self._postgres_pool.fetchrow(
            CREATE_BOARD_SQL,
            slug,
            title,
            description,
            position,
            is_active,
            is_nsfw,
            json.dumps(media_overrides),
        )

        if row is None:
            # insert с параметрами не вернёт пустую строку
            raise RuntimeError(f"board not created: {slug}")

        return _detail_record(row)

    async def list_all_boards(self) -> list[BoardDetailRecord]:
        """все доски с полными данными, включая неактивные"""

        rows = await self._postgres_pool.fetch(LIST_ALL_BOARDS_SQL)
        return [_detail_record(row) for row in rows]

    async def delete_board(self, slug: str) -> None:
        """удалить доску"""

        await self._postgres_pool.execute(DELETE_BOARD_SQL, slug)

    async def delete_board_with_threads(self, slug: str) -> int | None:
        """удалить доску и все её треды одной транзакцией"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(DELETE_BOARD_WITH_THREADS_SQL, slug)

        return None if row is None else row["id"]

    async def delete_board_with_threads_in_transaction(
        self, connection: Connection, slug: str
    ) -> int | None:
        """удалить доску уже внутри внешней транзакции"""

        row = await connection.fetchrow(DELETE_BOARD_WITH_THREADS_SQL, slug)
        return None if row is None else row["id"]

    async def update_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
        media_overrides: dict[str, object],
    ) -> BoardDetailRecord | None:
        """изменить доску, None если такая доска не найдена"""

        row = await self._postgres_pool.fetchrow(
            UPDATE_BOARD_SQL,
            slug,
            title,
            description,
            position,
            is_active,
            is_nsfw,
            json.dumps(media_overrides),
        )

        if row is None:
            return None

        return _detail_record(row)

    async def update_board_in_transaction(
        self, connection: Connection, **values: object
    ) -> BoardDetailRecord | None:
        """изменить доску уже внутри внешней транзакции"""

        row = await connection.fetchrow(
            UPDATE_BOARD_SQL,
            values["slug"],
            values["title"],
            values["description"],
            values["position"],
            values["is_active"],
            values["is_nsfw"],
            json.dumps(values["media_overrides"]),
        )
        return None if row is None else _detail_record(row)
