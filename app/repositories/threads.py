"""sql запросы для тредов и постов"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool, Record

# треды доски: закреплённые выше обычных, остальные - по последнему ответу
LIST_THREADS_PAGE_SQL = """
SELECT id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
FROM threads
WHERE board_id = $1
    AND is_archived = FALSE
    AND (
        $2::boolean IS NULL
        OR is_sticky < $2
        OR (
            is_sticky = $2
            AND (bumped_at, id) < ($3::timestamptz, $4::bigint)
        )
    )
ORDER BY is_sticky DESC, bumped_at DESC, id DESC
LIMIT $5
"""

GET_BOARD_ID_BY_SLUG_SQL = """
SELECT id
FROM boards
WHERE slug = $1 AND is_active = TRUE
"""

GET_THREAD_SQL = """
SELECT t.id, t.subject, t.is_sticky, t.is_locked, t.is_archived, t.created_at, t.bumped_at
FROM threads AS t
JOIN boards AS b ON b.id = t.board_id
WHERE t.id = $1 AND b.is_active = TRUE
"""

GET_THREAD_OP_SQL = """
SELECT id, author_name, body, created_at
FROM posts
WHERE thread_id = $1
ORDER BY id ASC
LIMIT 1
"""

GET_THREAD_REPLIES_PAGE_SQL = """
SELECT id, author_name, body, created_at
FROM posts
WHERE thread_id = $1
    AND id > $2
    AND ($3::bigint IS NULL OR id < $3)
ORDER BY id DESC
LIMIT $4
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

LOCK_THREAD_FOR_POST_SQL = """
SELECT is_locked, is_archived
FROM threads
WHERE id = $1
FOR UPDATE
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


@dataclass(frozen=True, slots=True)
class PostCreationResult:
    """результат создания поста внутри треда"""

    post: PostRecord
    was_bumped: bool


@dataclass(frozen=True, slots=True)
class ThreadCreationResult:
    """результат создания треда с его первым постом"""

    thread: ThreadRecord
    post: PostRecord


@dataclass(frozen=True, slots=True)
class ThreadPageRecord:
    """тред, OP и ответы одной страницы из postgresql"""

    thread: ThreadRecord
    op: PostRecord | None
    replies: list[PostRecord]


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


def _post_record(row: Record) -> PostRecord:
    """превратить строку базы в запись поста"""

    return PostRecord(
        id=row["id"],
        author_name=row["author_name"],
        body=row["body"],
        created_at=row["created_at"],
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

    async def list_threads_page(
        self,
        *,
        board_id: int,
        cursor_sticky: bool | None,
        cursor_bumped_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[ThreadRecord]:
        """вернуть страницу тредов в порядке показа"""

        rows = await self._postgres_pool.fetch(
            LIST_THREADS_PAGE_SQL,
            board_id,
            cursor_sticky,
            cursor_bumped_at,
            cursor_id,
            limit + 1,
        )
        return [_thread_record(row) for row in rows]

    async def get_thread(self, thread_id: int) -> ThreadRecord | None:
        """вернуть тред по id, None если нет"""

        row = await self._postgres_pool.fetchrow(GET_THREAD_SQL, thread_id)
        return None if row is None else _thread_record(row)

    async def get_thread_page(
        self,
        *,
        thread_id: int,
        before_id: int | None,
        limit: int,
    ) -> ThreadPageRecord | None:
        """вернуть OP и страницу последних или более старых ответов"""

        # один снимок чтения не даст вернуть удалённый тред с чужими постами
        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                thread_row = await connection.fetchrow(GET_THREAD_SQL, thread_id)
                if thread_row is None:
                    return None

                op_row = await connection.fetchrow(GET_THREAD_OP_SQL, thread_id)
                op = None if op_row is None else _post_record(op_row)

                # первый пост не должен повторяться среди ответов
                op_id = 0 if op is None else op.id
                reply_rows = await connection.fetch(
                    GET_THREAD_REPLIES_PAGE_SQL,
                    thread_id,
                    op_id,
                    before_id,
                    limit + 1,
                )

        return ThreadPageRecord(
            thread=_thread_record(thread_row),
            op=op,
            replies=[_post_record(row) for row in reply_rows],
        )

    async def create_thread(
        self,
        *,
        board_id: int,
        subject: str,
        author_name: str | None,
        body: str,
    ) -> ThreadCreationResult:
        """создать тред и первый пост в одной транзакции"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                thread_row = await connection.fetchrow(
                    CREATE_THREAD_SQL,
                    board_id,
                    subject,
                    False,
                )
                if thread_row is None:
                    # insert без ошибки всегда возвращает строку
                    raise RuntimeError(f"thread not created: board_id={board_id}")

                post_row = await connection.fetchrow(
                    CREATE_POST_SQL,
                    thread_row["id"],
                    author_name,
                    body,
                )
                if post_row is None:
                    # этот случай защищает от неполного ответа драйвера или базы
                    raise RuntimeError(f"opening post not created: thread_id={thread_row['id']}")

        return ThreadCreationResult(
            thread=_thread_record(thread_row),
            post=PostRecord(
                id=post_row["id"],
                author_name=author_name,
                body=body,
                created_at=post_row["created_at"],
            ),
        )

    async def create_post(
        self,
        *,
        thread_id: int,
        author_name: str | None,
        body: str,
        reference_ids: list[int],
    ) -> PostCreationResult | None:
        """создать пост и поднять доступный тред в одной транзакции"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # блокируем строку треда, чтобы состояние не изменилось до коммита
                thread_row = await connection.fetchrow(LOCK_THREAD_FOR_POST_SQL, thread_id)
                if thread_row is None:
                    return None

                if thread_row["is_locked"]:
                    raise ValueError("thread is locked")

                if thread_row["is_archived"]:
                    raise ValueError("thread is archived")

                post_row = await connection.fetchrow(
                    CREATE_POST_SQL, thread_id, author_name, body
                )

                # архивные треды выше уже отклонены, поэтому успешный пост всегда bump
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

        return PostCreationResult(
            post=PostRecord(
                id=post_row["id"],
                author_name=author_name,
                body=body,
                created_at=post_row["created_at"],
            ),
            was_bumped=True,
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
