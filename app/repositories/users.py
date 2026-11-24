"""sql запросы для сотрудников, сессий и журнала модерации"""

from dataclasses import dataclass
from datetime import datetime

from asyncpg import Pool, Record

GET_USER_BY_LOGIN_SQL = """
SELECT id, login, password_hash, role, created_at
FROM users
WHERE login = $1
"""

GET_USER_BY_SESSION_SQL = """
SELECT u.id, u.login, u.role, u.created_at
FROM users u
JOIN user_sessions s ON s.user_id = u.id
WHERE s.token_hash = $1 AND s.expires_at > now()
"""

CREATE_USER_SQL = """
INSERT INTO users (login, password_hash, role)
VALUES ($1, $2, $3)
RETURNING id, login, role, created_at
"""

CREATE_SESSION_SQL = """
INSERT INTO user_sessions (token_hash, user_id, expires_at)
VALUES ($1, $2, $3)
"""

DELETE_SESSION_SQL = """
DELETE FROM user_sessions WHERE token_hash = $1
"""

INSERT_MODERATION_SQL = """
INSERT INTO moderation_log (actor_id, action, target_type, target_id, reason)
VALUES ($1, $2, $3, $4, $5)
"""


@dataclass(frozen=True, slots=True)
class UserRow:
    """данные сотрудника вместе с хешем пароля"""

    id: int
    login: str
    password_hash: str
    role: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserRecord:
    """публичные данные сотрудника (без пароля)"""

    id: int
    login: str
    role: str
    created_at: datetime


def _to_record(row: Record) -> UserRecord:
    """превратить строку базы в запись сотрудника"""

    return UserRecord(
        id=row["id"],
        login=row["login"],
        role=row["role"],
        created_at=row["created_at"],
    )


class UsersRepository:
    """доступ к сотрудникам, сессиям и журналу модерации"""

    def __init__(self, postgres_pool: Pool) -> None:
        # сохраняем общий пул, а не открываем новое подключение
        self._postgres_pool = postgres_pool

    async def get_user_by_login(self, login: str) -> UserRow | None:
        """вернуть сотрудника по login"""

        row = await self._postgres_pool.fetchrow(GET_USER_BY_LOGIN_SQL, login)
        if row is None:
            return None

        return UserRow(
            id=row["id"],
            login=row["login"],
            password_hash=row["password_hash"],
            role=row["role"],
            created_at=row["created_at"],
        )

    async def get_user_by_session(self, token_hash: str) -> UserRecord | None:
        """вернуть сотрудника по хешу сессионного токена"""

        row = await self._postgres_pool.fetchrow(GET_USER_BY_SESSION_SQL, token_hash)
        return None if row is None else _to_record(row)

    async def create_user(self, login: str, password_hash: str, role: str) -> UserRecord:
        """создать сотрудника"""

        row = await self._postgres_pool.fetchrow(CREATE_USER_SQL, login, password_hash, role)
        if row is None:
            # insert без ошибки всегда возвращает строку
            raise RuntimeError(f"user not created: {login}")

        return _to_record(row)

    async def create_session(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        """создать сессию"""

        await self._postgres_pool.execute(CREATE_SESSION_SQL, token_hash, user_id, expires_at)

    async def delete_session(self, token_hash: str) -> None:
        """удалить сессию"""

        await self._postgres_pool.execute(DELETE_SESSION_SQL, token_hash)

    async def log_moderation(
        self,
        actor_id: int | None,
        action: str,
        target_type: str,
        target_id: int,
        reason: str | None = None,
    ) -> None:
        """записать действие в журнал модерации"""

        await self._postgres_pool.execute(
            INSERT_MODERATION_SQL, actor_id, action, target_type, target_id, reason
        )
