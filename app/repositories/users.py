"""sql запросы для сотрудников, сессий и журнала модерации"""

import json
from dataclasses import dataclass
from datetime import datetime

from asyncpg import Connection, Pool, Record

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

CREATE_PROFILE_USER_SQL = """
WITH new_user AS (
    INSERT INTO users (login, password_hash, role)
    VALUES ($1, $2, 'user')
    RETURNING id, login, role, created_at
), new_profile_board AS (
    INSERT INTO boards (
        slug, title, description, rules, board_type, owner_user_id,
        thread_creation_policy, reply_policy
    )
    SELECT login, login, '', '', 'profile', id, 'owner_only', 'authenticated'
    FROM new_user
)
SELECT id, login, role, created_at FROM new_user
"""

CREATE_SESSION_SQL = """
INSERT INTO user_sessions (token_hash, user_id, expires_at)
VALUES ($1, $2, $3)
"""

DELETE_SESSION_SQL = """
DELETE FROM user_sessions WHERE token_hash = $1
"""

DELETE_EXPIRED_SESSIONS_SQL = """
DELETE FROM user_sessions
WHERE expires_at <= now()
"""

INSERT_MODERATION_SQL = """
INSERT INTO moderation_log (actor_id, action, target_type, target_id, reason, metadata)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
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

    async def create_profile_user(self, handle: str, password_hash: str) -> UserRecord:
        """создать обычный аккаунт и profile-доску одним sql выражением"""

        # cte откатит и пользователя, и доску при конфликте handle или ошибке вставки
        row = await self._postgres_pool.fetchrow(CREATE_PROFILE_USER_SQL, handle, password_hash)
        if row is None:
            raise RuntimeError(f"profile user not created: {handle}")
        return _to_record(row)

    async def create_session(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        """создать сессию"""

        await self._postgres_pool.execute(CREATE_SESSION_SQL, token_hash, user_id, expires_at)

    async def delete_session(self, token_hash: str) -> None:
        """удалить сессию"""

        await self._postgres_pool.execute(DELETE_SESSION_SQL, token_hash)

    async def delete_expired_sessions(self) -> int:
        """удалить истёкшие staff-сессии"""

        # postgres отвечает строкой DELETE 3, оставляем только число
        result = await self._postgres_pool.execute(DELETE_EXPIRED_SESSIONS_SQL)
        return int(result.removeprefix("DELETE "))

    async def log_moderation(
        self,
        actor_id: int | None,
        action: str,
        target_type: str,
        target_id: int,
        reason: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """записать действие в журнал модерации"""

        await self._postgres_pool.execute(
            INSERT_MODERATION_SQL, actor_id, action, target_type, target_id, reason,
            json.dumps(metadata or {}),
        )

    async def log_moderation_in_transaction(
        self, connection: Connection, actor_id: int | None, action: str, target_type: str,
        target_id: int, reason: str | None = None, metadata: dict[str, object] | None = None,
    ) -> None:
        """записать действие в уже открытую транзакцию"""

        await connection.execute(
            INSERT_MODERATION_SQL, actor_id, action, target_type, target_id, reason,
            json.dumps(metadata or {}),
        )
