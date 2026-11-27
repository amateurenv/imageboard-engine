"""sql для одноразовых прав подготовки медиа"""

# ruff: noqa: E501 - SQL-запросы оставлены целыми для безопасной проверки условий

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from asyncpg import Connection, Pool, Record


@dataclass(frozen=True, slots=True)
class ClaimBoard:
    id: int
    board_type: str
    owner_user_id: int | None
    thread_creation_policy: str
    reply_policy: str
    media_overrides: dict[str, object]


@dataclass(frozen=True, slots=True)
class PostingClaim:
    id: int
    board_id: int
    account_user_id: int | None
    expires_at: datetime
    consumed_at: datetime | None
    max_attachments: int
    sticker_thread_id: int | None = None


@dataclass(frozen=True, slots=True)
class ClaimMedia:
    id: int
    size: int
    mime_type: str
    is_animation: bool
    media_kind: str


def _claim(row: Record) -> PostingClaim:
    return PostingClaim(
        row["id"], row["board_id"], row["account_user_id"], row["expires_at"],
        row["consumed_at"], row["max_attachments"],
        row["sticker_thread_id"],
    )


class PostingClaimsRepository:
    """блокировки claim и временных media внутри одной транзакции"""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def get_board(self, slug: str) -> ClaimBoard | None:
        row = await self._pool.fetchrow(
            """SELECT id, board_type, owner_user_id, thread_creation_policy, reply_policy,
                      media_overrides
               FROM boards WHERE slug=$1 AND is_active=TRUE""",
            slug,
        )
        if row is None:
            return None
        raw = row["media_overrides"]
        return ClaimBoard(
            row["id"], row["board_type"], row["owner_user_id"],
            row["thread_creation_policy"], row["reply_policy"],
            raw if isinstance(raw, dict) else json.loads(raw),
        )

    async def get_board_by_thread(self, thread_id: int) -> ClaimBoard | None:
        """получить доску треда для повторной проверки policy"""

        row = await self._pool.fetchrow(
            """SELECT b.id, b.board_type, b.owner_user_id, b.thread_creation_policy,
                      b.reply_policy, b.media_overrides
               FROM boards AS b JOIN threads AS t ON t.board_id=b.id
               WHERE t.id=$1 AND b.is_active=TRUE""",
            thread_id,
        )
        if row is None:
            return None
        raw = row["media_overrides"]
        return ClaimBoard(
            row["id"], row["board_type"], row["owner_user_id"],
            row["thread_creation_policy"], row["reply_policy"],
            raw if isinstance(raw, dict) else json.loads(raw),
        )

    async def lock_sticker_target(self, connection: Connection, thread_id: int) -> ClaimBoard | None:
        """зафиксировать правила доски и видимость треда до окончания транзакции"""

        # удаление доски блокирует доску перед тредами - здесь сохраняем тот же порядок
        row = await connection.fetchrow(
            """SELECT id, board_type, owner_user_id, thread_creation_policy, reply_policy,
                      media_overrides
               FROM boards WHERE is_active AND id=(SELECT board_id FROM threads WHERE id=$1)
               FOR SHARE""",
            thread_id,
        )
        if row is None:
            return None
        target = await connection.fetchval(
            """SELECT id FROM threads
               WHERE id=$1 AND board_id=$2 AND NOT is_hidden AND NOT is_locked AND NOT is_archived
               FOR UPDATE""",
            thread_id, row["id"],
        )
        if target is None:
            return None
        raw = row["media_overrides"]
        return ClaimBoard(
            row["id"], row["board_type"], row["owner_user_id"],
            row["thread_creation_policy"], row["reply_policy"],
            raw if isinstance(raw, dict) else json.loads(raw),
        )

    async def create_sticker_claim(
        self, connection: Connection, board_id: int, thread_id: int, actor_id: int,
        token_hash: str, ttl: timedelta,
    ) -> int:
        """создать одноразовый claim на один стикер для конкретного ответа"""

        claim_id: int = await connection.fetchval(
            """INSERT INTO posting_claims
               (board_id, account_user_id, token_hash, expires_at, max_attachments, sticker_thread_id)
               VALUES ($1,$2,$3,clock_timestamp()+$4::interval,1,$5) RETURNING id""",
            board_id, actor_id, token_hash, ttl, thread_id,
        )
        return claim_id

    async def create(
        self, board_id: int, account_user_id: int | None, token_hash: str,
        ttl: timedelta, max_attachments: int,
    ) -> PostingClaim:
        row = await self._pool.fetchrow(
            """INSERT INTO posting_claims
               (board_id, account_user_id, token_hash, expires_at, max_attachments)
               VALUES ($1,$2,$3,clock_timestamp()+$4::interval,$5)
               RETURNING id,board_id,account_user_id,expires_at,consumed_at,max_attachments,
                         sticker_thread_id""",
            board_id, account_user_id, token_hash, ttl, max_attachments,
        )
        if row is None:
            raise RuntimeError("posting claim not created")
        return _claim(row)

    async def get_claim(self, token_hash: str) -> PostingClaim | None:
        """прочитать claim перед проверкой содержимого файла"""

        row = await self._pool.fetchrow(
            """SELECT id,board_id,account_user_id,expires_at,consumed_at,max_attachments,sticker_thread_id
               FROM posting_claims WHERE token_hash=$1""", token_hash,
        )
        return None if row is None else _claim(row)

    async def lock_claim(self, connection: Connection, token_hash: str) -> PostingClaim | None:
        row = await connection.fetchrow(
            """SELECT id,board_id,account_user_id,expires_at,consumed_at,max_attachments,sticker_thread_id
               FROM posting_claims WHERE token_hash=$1 FOR UPDATE""", token_hash
        )
        return None if row is None else _claim(row)

    async def lock_board(self, connection: Connection, slug: str) -> ClaimBoard | None:
        """заблокировать доску вместе с claim, чтобы policy не менялась посередине upload"""

        row = await connection.fetchrow(
            """SELECT id, board_type, owner_user_id, thread_creation_policy, reply_policy,
                      media_overrides
               FROM boards WHERE slug=$1 AND is_active=TRUE FOR UPDATE""",
            slug,
        )
        if row is None:
            return None
        raw = row["media_overrides"]
        return ClaimBoard(
            row["id"], row["board_type"], row["owner_user_id"],
            row["thread_creation_policy"], row["reply_policy"],
            raw if isinstance(raw, dict) else json.loads(raw),
        )

    async def lock_unattached_media_sizes(
        self, connection: Connection, claim_id: int
    ) -> list[int]:
        """прочитать уже загруженные файлы claim под той же блокировкой"""

        rows = await connection.fetch(
            """SELECT id, size FROM media
               WHERE posting_claim_id=$1 AND post_id IS NULL
               ORDER BY id FOR UPDATE""",
            claim_id,
        )
        return [row["size"] for row in rows]

    async def lock_media(
        self, connection: Connection, claim_id: int, board_id: int, media_ids: list[int]
    ) -> list[ClaimMedia]:
        rows = await connection.fetch(
            """SELECT id,size,mime_type,is_animation,media_kind FROM media
               WHERE id = ANY($1::bigint[]) AND posting_claim_id=$2 AND board_id=$3
                 AND post_id IS NULL AND processing_state='ready'
               ORDER BY array_position($1::bigint[], id) FOR UPDATE""",
            media_ids, claim_id, board_id,
        )
        return [
            ClaimMedia(
                row["id"], row["size"], row["mime_type"], row["is_animation"], row["media_kind"]
            )
            for row in rows
        ]

    async def attach_and_consume(
        self,
        connection: Connection,
        claim_id: int,
        post_id: int,
        media_ids: list[int],
        public_keys: dict[int, str],
    ) -> None:
        """прикрепить только ready media и выдать opaque key в той же транзакции"""

        for position, media_id in enumerate(media_ids):
            result = await connection.execute(
                """UPDATE media AS media
                   SET post_id=$2,
                       attachment_position=$3,
                       public_key=CASE
                           WHEN media.media_kind='image'
                            AND media.display_path IS NOT NULL
                            AND media.preview_path IS NOT NULL
                           THEN $5
                           WHEN media.media_kind='sticker'
                            AND media.display_path IS NOT NULL
                           THEN $5
                           WHEN media.media_kind IN ('audio', 'voice')
                            AND media.canonical_path IS NOT NULL
                            AND media.canonical_mime_type IS NOT NULL
                           THEN $5
                           WHEN media.media_kind IN ('video', 'video_note')
                            AND media.canonical_path IS NOT NULL
                            AND media.canonical_mime_type = 'video/mp4'
                            AND media.preview_path IS NOT NULL
                           THEN $5
                           ELSE NULL
                       END
                   WHERE media.id=$1 AND media.posting_claim_id=$4
                     AND media.post_id IS NULL AND media.processing_state='ready'
                     AND EXISTS (
                         SELECT 1
                         FROM posts AS post
                         JOIN threads AS thread ON thread.id=post.thread_id
                         JOIN boards AS board ON board.id=thread.board_id
                         WHERE post.id=$2 AND post.is_hidden=FALSE
                           AND thread.is_hidden=FALSE AND board.is_active=TRUE
                     )""",
                media_id, post_id, position, claim_id, public_keys[media_id],
            )
            if result != "UPDATE 1":
                raise RuntimeError("media attachment changed during claim consumption")
        result = await connection.execute(
            "UPDATE posting_claims SET consumed_at=clock_timestamp() WHERE id=$1 AND consumed_at IS NULL",
            claim_id,
        )
        if result != "UPDATE 1":
            raise RuntimeError("posting claim already consumed")

    async def cleanup_expired(self) -> int:
        """удалить только неиспользованные media истёкших claims"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """WITH expired AS (
                           SELECT id FROM posting_claims
                           WHERE consumed_at IS NULL AND expires_at <= clock_timestamp()
                           FOR UPDATE
                       ), removed_media AS (
                           DELETE FROM media WHERE posting_claim_id IN (SELECT id FROM expired)
                           AND post_id IS NULL
                       )
                       DELETE FROM posting_claims WHERE id IN (SELECT id FROM expired)"""
                )
        return int(result.removeprefix("DELETE "))
