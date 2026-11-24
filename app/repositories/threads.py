"""sql запросы для тредов и постов"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool, Record

# треды доски: закреплённые выше обычных, остальные — по последнему ответу
LIST_THREADS_SQL = """
SELECT id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
FROM threads
WHERE board_id = $1
    AND is_archived = FALSE
ORDER BY is_sticky DESC, bumped_at DESC, id DESC
"""

GET_BOARD_ID_BY_SLUG_SQL = """
SELECT id FROM boards WHERE slug = $1
"""

GET_THREAD_SQL = """
SELECT id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
FROM threads
WHERE id = $1
"""

GET_THREAD_POSTS_SQL = """
SELECT id, author_name, body, created_at
FROM posts
WHERE thread_id = $1
ORDER BY id ASC
"""

CREATE_THREAD_SQL = """
INSERT INTO threads (board_id, subject, is_sticky)
VALUES ($1, $2, $3)
RETURNING id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
"""

UPDATE_THREAD_SQL = """
UPDATE threads
SET subject = $2, is_sticky = $3, is_locked = $4, is_archived = $5
WHERE id = $1
RETURNING id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
"""

CREATE_POST_SQL = """
INSERT INTO posts (thread_id, author_name, body)
VALUES ($1, $2, $3)
RETURNING id, created_at
"""

BUMP_THREAD_SQL = """
UPDATE threads SET bumped_at = now() WHERE id = $1
"""

INSERT_POST_REFERENCE_SQL = """
INSERT INTO post_references (source_post_id, target_post_id)
VALUES ($1, $2)
ON CONFLICT DO NOTHING
"""


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """данные одного треда, полученные из postgresql"""

    id: int
    subject: str
    is_sticky: bool
    is_locked: bool
    is_archived: bool
    created_at: datetime
    bumped_at: datetime


@dataclass(frozen=True, slots=True)
class PostRecord:
    """данные одного поста, полученные из postgresql"""

    id: int
    author_name: str | None
    body: str
    created_at: datetime


def _thread_record(row: Record) -> ThreadRecord:
    """превратить строку базы в запись треда"""

    return ThreadRecord(
        id=row["id"],
        subject=row["subject"],
        is_sticky=row["is_sticky"],
        is_locked=row["is_locked"],
        is_archived=row["is_archived"],
        created_at=row["created_at"],
        bumped_at=row["bumped_at"],
    )


class ThreadsRepository:
    """чтение и запись тредов и постов в postgresql"""

    def __init__(self, postgres_pool: Pool) -> None:
        # сохраняем общий пул, а не открываем новое подключение
        self._postgres_pool = postgres_pool

    async def get_board_id_by_slug(self, slug: str) -> int | None:
        """вернуть id доски по slug, None если доски нет"""

        row = await self._postgres_pool.fetchrow(GET_BOARD_ID_BY_SLUG_SQL, slug)
        return None if row is None else row["id"]

    async def list_threads(self, board_id: int) -> list[ThreadRecord]:
        """список тредов доски: закреплённые выше"""

        rows = await self._postgres_pool.fetch(LIST_THREADS_SQL, board_id)
        return [_thread_record(row) for row in rows]

    async def get_thread(self, thread_id: int) -> ThreadRecord | None:
        """вернуть тред по id, None если нет"""

        row = await self._postgres_pool.fetchrow(GET_THREAD_SQL, thread_id)
        return None if row is None else _thread_record(row)

    async def get_posts(self, thread_id: int) -> list[PostRecord]:
        """все посты треда по порядку"""

        rows = await self._postgres_pool.fetch(GET_THREAD_POSTS_SQL, thread_id)
        return [
            PostRecord(
                id=row["id"],
                author_name=row["author_name"],
                body=row["body"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def create_thread(
        self, *, board_id: int, subject: str, is_sticky: bool
    ) -> ThreadRecord:
        """создать тред"""

        row = await self._postgres_pool.fetchrow(
            CREATE_THREAD_SQL, board_id, subject, is_sticky
        )
        if row is None:
            # insert без ошибки всегда возвращает строку
            raise RuntimeError(f"thread not created: board_id={board_id}")
        return _thread_record(row)

    async def create_post(
        self,
        *,
        thread_id: int,
        author_name: str | None,
        body: str,
        reference_ids: list[int],
    ) -> PostRecord:
        """создать пост и поднять тред, всё в одной транзакции"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                post_row = await connection.fetchrow(
                    CREATE_POST_SQL, thread_id, author_name, body
                )
                # архивный тред не поднимается новыми ответами
                is_archived = await connection.fetchval(
                    "SELECT is_archived FROM threads WHERE id = $1", thread_id
                )
                if not is_archived:
                    await connection.execute(BUMP_THREAD_SQL, thread_id)
                if reference_ids:
                    # ссылки ведём только на посты, которые уже есть в базе
                    existing_rows = await connection.fetch(
                        "SELECT id FROM posts WHERE id = ANY($1::bigint[])",
                        reference_ids,
                    )
                    existing_ids = {row["id"] for row in existing_rows}
                    for target_id in reference_ids:
                        if target_id in existing_ids and target_id != post_row["id"]:
                            await connection.execute(
                                INSERT_POST_REFERENCE_SQL,
                                post_row["id"],
                                target_id,
                            )

    async def update_thread(
        self,
        *,
        thread_id: int,
        subject: str,
        is_sticky: bool,
        is_locked: bool,
        is_archived: bool,
    ) -> ThreadRecord | None:
        """изменить тред"""

        row = await self._postgres_pool.fetchrow(
            UPDATE_THREAD_SQL, thread_id, subject, is_sticky, is_locked, is_archived
        )
        return None if row is None else _thread_record(row)

    async def delete_thread(self, thread_id: int) -> bool:
        """удалить тред, вернуть есть ли такой"""

        row = await self._postgres_pool.fetchrow(
            "DELETE FROM threads WHERE id = $1 RETURNING id", thread_id
        )
        return row is not None

    async def delete_post(self, post_id: int) -> bool:
        """удалить пост, вернуть есть ли такой"""

        row = await self._postgres_pool.fetchrow(
            "DELETE FROM posts WHERE id = $1 RETURNING id", post_id
        )
        return row is not None
