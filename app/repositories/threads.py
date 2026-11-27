"""sql запросы для тредов и постов"""

from dataclasses import dataclass, replace
from datetime import datetime

from asyncpg import Connection, Pool, Record

# треды доски: закреплённые выше обычных, остальные - по последнему ответу
LIST_THREADS_PAGE_SQL = """
SELECT t.id, t.thread_number, t.subject, t.is_sticky, t.is_locked, t.is_archived,
       t.created_at, t.bumped_at, b.slug AS board_slug, b.board_type
FROM threads AS t JOIN boards AS b ON b.id = t.board_id
WHERE t.board_id = $1
    AND t.is_archived = FALSE AND t.is_hidden = FALSE
    AND (
        $2::boolean IS NULL
        OR t.is_sticky < $2
        OR (
            t.is_sticky = $2
            AND (bumped_at, id) < ($3::timestamptz, $4::bigint)
        )
    )
ORDER BY t.is_sticky DESC, t.bumped_at DESC, t.id DESC
LIMIT $5
"""

GET_BOARD_ID_BY_SLUG_SQL = """
SELECT id
FROM boards
WHERE slug = $1 AND is_active = TRUE
"""

GET_BOARD_POSTING_SQL = """
SELECT id, board_type, owner_user_id, thread_creation_policy, reply_policy
FROM boards WHERE slug = $1 AND is_active = TRUE
"""

GET_THREAD_POSTING_SQL = """
SELECT b.id, b.board_type, b.owner_user_id, b.thread_creation_policy, b.reply_policy
FROM threads AS t JOIN boards AS b ON b.id = t.board_id
WHERE t.id = $1 AND b.is_active = TRUE AND t.is_hidden = FALSE
"""

GET_THREAD_SQL = """
SELECT t.id, t.thread_number, t.subject, t.is_sticky, t.is_locked, t.is_archived,
       t.created_at, t.bumped_at, b.slug AS board_slug, b.board_type
FROM threads AS t
JOIN boards AS b ON b.id = t.board_id
WHERE t.id = $1 AND b.is_active = TRUE
"""

# публичное чтение не должно раскрывать скрытый тред, но модерации нужен полный запрос
GET_VISIBLE_THREAD_SQL = f"{GET_THREAD_SQL.rstrip()} AND t.is_hidden = FALSE"

GET_THREAD_OP_SQL = """
SELECT id, author_name, body, created_at
FROM posts
WHERE thread_id = $1
    AND is_hidden = FALSE
ORDER BY id ASC
LIMIT 1
"""

GET_THREAD_REPLIES_PAGE_SQL = """
SELECT id, author_name, body, created_at
FROM posts
WHERE thread_id = $1
    AND is_hidden = FALSE
    AND id > $2
    AND ($3::bigint IS NULL OR id < $3)
ORDER BY id DESC
LIMIT $4
"""

CREATE_THREAD_SQL = """
WITH allocated_number AS (
    UPDATE boards
    SET next_thread_number = next_thread_number + 1
    WHERE id = $1
    RETURNING next_thread_number - 1 AS thread_number
)
INSERT INTO threads (board_id, thread_number, subject, is_sticky)
SELECT $1, thread_number, $2, $3 FROM allocated_number
RETURNING id, thread_number, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
"""

UPDATE_THREAD_SQL = """
UPDATE threads
SET subject = $2, is_sticky = $3, is_locked = $4, is_archived = $5
WHERE id = $1
RETURNING id, subject, is_sticky, is_locked, is_archived, created_at, bumped_at
"""

CREATE_POST_SQL = """
INSERT INTO posts (thread_id, author_name, body, author_user_id, deletion_password_hash)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, created_at
"""

GET_THREAD_SELF_DELETE_SQL = """
SELECT t.id AS thread_id, t.board_id, p.id AS post_id, p.author_user_id,
       p.deletion_password_hash
FROM threads AS t
JOIN LATERAL (
    SELECT id, author_user_id, deletion_password_hash
    FROM posts WHERE thread_id = t.id ORDER BY id ASC LIMIT 1
) AS p ON TRUE
WHERE t.id = $1
FOR UPDATE OF t, p
"""

GET_POST_SELF_DELETE_SQL = """
SELECT p.id AS post_id, p.thread_id, t.board_id, p.author_user_id,
       p.deletion_password_hash,
       p.id = (
           SELECT id FROM posts WHERE thread_id = p.thread_id ORDER BY id ASC LIMIT 1
       ) AS is_opening
FROM posts AS p
JOIN threads AS t ON t.id = p.thread_id
WHERE p.id = $1
FOR UPDATE OF p, t
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

LIST_PUBLIC_MEDIA_SQL = """
SELECT media.post_id, media.public_key, media.mime_type
FROM media
JOIN posts AS post ON post.id=media.post_id
JOIN threads AS thread ON thread.id=post.thread_id
JOIN boards AS board ON board.id=thread.board_id
WHERE media.post_id = ANY($1::bigint[])
  AND media.processing_state='ready'
  AND media.public_key IS NOT NULL
  AND media.display_path IS NOT NULL
  AND media.preview_path IS NOT NULL
  AND post.is_hidden=FALSE
  AND thread.is_hidden=FALSE
  AND board.is_active=TRUE
ORDER BY media.attachment_position ASC NULLS LAST, media.id ASC
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
    thread_number: int = 0
    board_slug: str = ""
    board_type: str = "public"


@dataclass(frozen=True, slots=True)
class PostingBoardRecord:
    """данные доски, нужные только для проверки policy публикации"""

    id: int
    board_type: str
    owner_user_id: int | None
    thread_creation_policy: str
    reply_policy: str


@dataclass(frozen=True, slots=True)
class PublicMediaRecord:
    """только opaque key и mime ready derivative для публичного ответа поста"""

    public_key: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class PostRecord:
    """данные одного поста, полученные из postgresql"""

    id: int
    author_name: str | None
    body: str
    created_at: datetime
    media: tuple[PublicMediaRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class PostCreationResult:
    """результат создания поста внутри треда"""

    post: PostRecord
    was_bumped: bool


@dataclass(frozen=True, slots=True)
class SelfDeleteThreadRecord:
    """минимум данных для проверки удаления треда"""

    thread_id: int
    board_id: int
    post_id: int
    author_user_id: int | None
    deletion_password_hash: str | None


@dataclass(frozen=True, slots=True)
class SelfDeletePostRecord:
    """минимум данных для проверки удаления одного ответа"""

    post_id: int
    thread_id: int
    board_id: int
    author_user_id: int | None
    deletion_password_hash: str | None
    is_opening: bool


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
        thread_number=row["thread_number"],
        board_slug=_optional_row_value(row, "board_slug", ""),
        board_type=_optional_row_value(row, "board_type", "public"),
    )


def _optional_row_value(row: Record, key: str, default: str) -> str:
    """прочитать поле join, сохранив совместимость старого unit-теста"""

    try:
        return row[key]
    except KeyError:
        return default


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

    async def get_board_posting(self, slug: str) -> PostingBoardRecord | None:
        """прочитать policy доски перед созданием треда"""

        row = await self._postgres_pool.fetchrow(GET_BOARD_POSTING_SQL, slug)
        return None if row is None else PostingBoardRecord(**dict(row))

    async def get_thread_posting(self, thread_id: int) -> PostingBoardRecord | None:
        """прочитать policy доски треда перед созданием ответа"""

        row = await self._postgres_pool.fetchrow(GET_THREAD_POSTING_SQL, thread_id)
        return None if row is None else PostingBoardRecord(**dict(row))

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

        row = await self._postgres_pool.fetchrow(GET_VISIBLE_THREAD_SQL, thread_id)
        return None if row is None else _thread_record(row)

    async def get_thread_for_update(
        self, connection: Connection, thread_id: int
    ) -> ThreadRecord | None:
        """прочитать тред с блокировкой для частичного изменения"""

        row = await connection.fetchrow(f"{GET_THREAD_SQL} FOR UPDATE", thread_id)
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
                thread_row = await connection.fetchrow(GET_VISIBLE_THREAD_SQL, thread_id)
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

        reply_records = [_post_record(row) for row in reply_rows]
        records = ([op] if op is not None else []) + reply_records
        media_by_post = await self.get_public_media_for_posts([record.id for record in records])
        return ThreadPageRecord(
            thread=_thread_record(thread_row),
            op=None if op is None else replace(op, media=media_by_post.get(op.id, ())),
            replies=[
                replace(record, media=media_by_post.get(record.id, ()))
                for record in reply_records
            ],
        )

    async def get_public_media_for_posts(
        self, post_ids: list[int]
    ) -> dict[int, tuple[PublicMediaRecord, ...]]:
        """получить все публичные derivative одним запросом без N+1"""

        if not post_ids:
            return {}
        rows = await self._postgres_pool.fetch(LIST_PUBLIC_MEDIA_SQL, post_ids)
        grouped: dict[int, list[PublicMediaRecord]] = {}
        for row in rows:
            grouped.setdefault(row["post_id"], []).append(
                PublicMediaRecord(row["public_key"], row["mime_type"])
            )
        return {post_id: tuple(media) for post_id, media in grouped.items()}

    async def create_thread(
        self,
        *,
        board_id: int,
        subject: str,
        author_name: str | None,
        body: str,
        author_user_id: int | None = None,
        deletion_password_hash: str | None = None,
    ) -> ThreadCreationResult:
        """создать тред и первый пост в одной транзакции"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                return await self.create_thread_in_transaction(
                    connection,
                    board_id=board_id,
                    subject=subject,
                    author_name=author_name,
                    body=body,
                    author_user_id=author_user_id,
                    deletion_password_hash=deletion_password_hash,
                )

    async def create_thread_in_transaction(
        self,
        connection: Connection,
        *,
        board_id: int,
        subject: str,
        author_name: str | None,
        body: str,
        author_user_id: int | None,
        deletion_password_hash: str | None,
    ) -> ThreadCreationResult:
        """создать тред внутри внешней транзакции claim или moderation"""

        thread_row = await connection.fetchrow(CREATE_THREAD_SQL, board_id, subject, False)
        if thread_row is None:
            raise RuntimeError(f"thread not created: board_id={board_id}")
        post_row = await connection.fetchrow(
            CREATE_POST_SQL,
            thread_row["id"],
            author_name,
            body,
            author_user_id,
            deletion_password_hash,
        )
        if post_row is None:
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
        author_user_id: int | None = None,
        deletion_password_hash: str | None = None,
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

                return await self.create_post_in_transaction(
                    connection,
                    thread_id=thread_id,
                    author_name=author_name,
                    body=body,
                    reference_ids=reference_ids,
                    author_user_id=author_user_id,
                    deletion_password_hash=deletion_password_hash,
                )

    async def create_post_in_transaction(
        self,
        connection: Connection,
        *,
        thread_id: int,
        author_name: str | None,
        body: str,
        reference_ids: list[int],
        author_user_id: int | None,
        deletion_password_hash: str | None,
    ) -> PostCreationResult:
        """создать ответ внутри уже заблокированной транзакции"""

        post_row = await connection.fetchrow(
            CREATE_POST_SQL, thread_id, author_name, body, author_user_id, deletion_password_hash
        )
        await connection.execute(BUMP_THREAD_SQL, thread_id)
        if reference_ids:
            existing_rows = await connection.fetch(
                "SELECT id FROM posts WHERE id = ANY($1::bigint[])", reference_ids
            )
            existing_ids = {row["id"] for row in existing_rows}
            for target_id in reference_ids:
                if target_id in existing_ids and target_id != post_row["id"]:
                    await connection.execute(INSERT_POST_REFERENCE_SQL, post_row["id"], target_id)
        return PostCreationResult(
            post=PostRecord(
                id=post_row["id"],
                author_name=author_name,
                body=body,
                created_at=post_row["created_at"],
            ),
            was_bumped=True,
        )

    async def get_thread_for_self_delete(
        self, connection: Connection, thread_id: int
    ) -> SelfDeleteThreadRecord | None:
        """заблокировать тред и его op перед проверкой автора"""

        row = await connection.fetchrow(GET_THREAD_SELF_DELETE_SQL, thread_id)
        if row is None:
            return None
        return SelfDeleteThreadRecord(
            thread_id=row["thread_id"],
            board_id=row["board_id"],
            post_id=row["post_id"],
            author_user_id=row["author_user_id"],
            deletion_password_hash=row["deletion_password_hash"],
        )

    async def get_post_for_self_delete(
        self, connection: Connection, post_id: int
    ) -> SelfDeletePostRecord | None:
        """заблокировать ответ и тред, чтобы op не сменился в гонке"""

        row = await connection.fetchrow(GET_POST_SELF_DELETE_SQL, post_id)
        if row is None:
            return None
        return SelfDeletePostRecord(
            post_id=row["post_id"],
            thread_id=row["thread_id"],
            board_id=row["board_id"],
            author_user_id=row["author_user_id"],
            deletion_password_hash=row["deletion_password_hash"],
            is_opening=row["is_opening"],
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

    async def update_thread_in_transaction(
        self, connection: Connection, **values: object
    ) -> ThreadRecord | None:
        """изменить тред уже внутри внешней транзакции"""

        row = await connection.fetchrow(
            UPDATE_THREAD_SQL,
            values["thread_id"],
            values["subject"],
            values["is_sticky"],
            values["is_locked"],
            values["is_archived"],
        )
        return None if row is None else _thread_record(row)

    async def delete_thread(self, thread_id: int) -> bool:
        """удалить тред, вернуть есть ли такой"""

        row = await self._postgres_pool.fetchrow(
            "DELETE FROM threads WHERE id = $1 RETURNING id", thread_id
        )
        return row is not None

    async def delete_thread_in_transaction(self, connection: Connection, thread_id: int) -> bool:
        """удалить тред уже внутри внешней транзакции"""

        row = await connection.fetchrow("DELETE FROM threads WHERE id = $1 RETURNING id", thread_id)
        return row is not None

    async def delete_post(self, post_id: int) -> bool:
        """удалить пост, вернуть есть ли такой"""

        row = await self._postgres_pool.fetchrow(
            "DELETE FROM posts WHERE id = $1 RETURNING id", post_id
        )
        return row is not None

    async def delete_non_opening_post_in_transaction(
        self, connection: Connection, post_id: int
    ) -> bool:
        """удалить пост, но запретить удаление первого поста треда"""

        row = await connection.fetchrow(
            """
            WITH target AS (
                SELECT id, thread_id FROM posts WHERE id = $1 FOR UPDATE
            ), opening_post AS (
                SELECT id FROM posts WHERE thread_id = (SELECT thread_id FROM target)
                ORDER BY id ASC LIMIT 1
            )
            DELETE FROM posts
            WHERE id = (SELECT id FROM target) AND id <> (SELECT id FROM opening_post)
            RETURNING id
            """,
            post_id,
        )
        return row is not None

    async def is_opening_post_in_transaction(
        self, connection: Connection, post_id: int
    ) -> bool:
        """проверить, является ли пост первым в треде"""

        row = await connection.fetchrow(
            """
            SELECT p.id = (
                SELECT id FROM posts WHERE thread_id = p.thread_id ORDER BY id ASC LIMIT 1
            ) AS is_opening
            FROM posts AS p WHERE p.id = $1 FOR UPDATE
            """,
            post_id,
        )
        return row is not None and row["is_opening"]
