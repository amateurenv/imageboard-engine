"""sql доступ к личным доскам и их владельцам"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool, Record

GET_PROFILE_SQL = """
SELECT b.id, b.slug, b.title, b.description, b.rules, b.thread_creation_policy,
       b.reply_policy, b.created_at, b.updated_at, b.owner_user_id
FROM boards AS b
WHERE b.board_type = 'profile' AND b.slug = $1 AND b.is_active = TRUE
"""

UPDATE_PROFILE_SQL = """
UPDATE boards
SET title = $3, description = $4, rules = $5, thread_creation_policy = $6,
    reply_policy = $7, updated_at = now()
WHERE board_type = 'profile' AND slug = $1 AND owner_user_id = $2
RETURNING id, slug, title, description, rules, thread_creation_policy,
          reply_policy, created_at, updated_at, owner_user_id
"""


@dataclass(frozen=True, slots=True)
class ProfileBoardRecord:
    """личная доска и id её владельца"""

    id: int
    handle: str
    name: str
    description: str
    rules: str
    thread_creation_policy: str
    reply_policy: str
    created_at: datetime
    updated_at: datetime
    owner_user_id: int


def _to_record(row: Record) -> ProfileBoardRecord:
    """превратить строку базы в типизированную profile-доску"""

    return ProfileBoardRecord(
        id=row["id"],
        handle=row["slug"],
        name=row["title"],
        description=row["description"],
        rules=row["rules"],
        thread_creation_policy=row["thread_creation_policy"],
        reply_policy=row["reply_policy"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        owner_user_id=row["owner_user_id"],
    )


class ProfilesRepository:
    """получение и изменение личных досок"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._postgres_pool = postgres_pool

    async def get_by_handle(self, handle: str) -> ProfileBoardRecord | None:
        """найти активную profile-доску по неизменяемому handle"""

        row = await self._postgres_pool.fetchrow(GET_PROFILE_SQL, handle)
        return None if row is None else _to_record(row)

    async def update_owned(
        self,
        *,
        handle: str,
        owner_user_id: int,
        name: str,
        description: str,
        rules: str,
        thread_creation_policy: str,
        reply_policy: str,
    ) -> ProfileBoardRecord | None:
        """изменить profile-доску только при совпадении владельца"""

        row = await self._postgres_pool.fetchrow(
            UPDATE_PROFILE_SQL,
            handle,
            owner_user_id,
            name,
            description,
            rules,
            thread_creation_policy,
            reply_policy,
        )
        return None if row is None else _to_record(row)
