"""sql запросы для банов по IP"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool, Record

GET_BY_IP_SQL = """
SELECT ip, reason, actor_id, created_at
FROM ip_bans
WHERE ip = $1
"""

LIST_ALL_SQL = """
SELECT ip, reason, actor_id, created_at
FROM ip_bans
ORDER BY created_at DESC
"""

CREATE_SQL = """
INSERT INTO ip_bans (ip, reason, actor_id)
VALUES ($1, $2, $3)
"""

DELETE_SQL = """
DELETE FROM ip_bans WHERE ip = $1
"""


@dataclass(frozen=True, slots=True)
class IpBanRecord:
    """данные бана по IP"""

    ip: str
    reason: str | None
    actor_id: int | None
    created_at: datetime


def _to_record(row: Record) -> IpBanRecord:
    """превратить строку базы в запись бана"""

    return IpBanRecord(
        ip=row["ip"],
        reason=row["reason"],
        actor_id=row["actor_id"],
        created_at=row["created_at"],
    )


class IpBansRepository:
    """доступ к ip_bans"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._postgres_pool = postgres_pool

    async def get_by_ip(self, ip: str) -> IpBanRecord | None:
        """вернуть бан для ip, None если нет"""

        row = await self._postgres_pool.fetchrow(GET_BY_IP_SQL, ip)
        return None if row is None else _to_record(row)

    async def list_all(self) -> list[IpBanRecord]:
        """вернуть все баны"""

        rows = await self._postgres_pool.fetch(LIST_ALL_SQL)
        return [_to_record(row) for row in rows]

    async def create(self, ip: str, reason: str | None, actor_id: int | None) -> IpBanRecord:
        """забанить ip"""

        await self._postgres_pool.execute(CREATE_SQL, ip, reason, actor_id)

        record = await self.get_by_ip(ip)
        if record is None:
            # insert без ошибки всегда возвращает строку
            raise RuntimeError(f"ip ban not created: {ip}")

        return record

    async def delete(self, ip: str) -> None:
        """снять бан"""

        await self._postgres_pool.execute(DELETE_SQL, ip)
