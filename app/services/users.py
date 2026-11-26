"""правила авторизации сотрудников"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from redis.asyncio import Redis

from app.core.security import (
    SESSION_TTL_SECONDS,
    generate_token,
    hash_token,
    verify_password,
)
from app.repositories.users import UserRow, UsersRepository
from app.schemas.users import UserPublic

# после скольки неудачных попыток входа из ip сессия блокируется
LOGIN_MAX_ATTEMPTS = 5

# сколько секунд действует блокировка после неудачных попыток
LOGIN_BLOCK_TTL_SECONDS = 600

# redis выполняет скрипт целиком, поэтому параллельные ошибки не теряют счётчик
RECORD_FAILED_LOGIN_SCRIPT = """
local failures = redis.call('INCR', KEYS[1])
if failures == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return failures
"""


class UsersService:
    """авторизация, сессии и доступ сотрудников"""

    def __init__(self, repository: UsersRepository, redis_client: Redis) -> None:
        self._repository = repository
        self._redis_client = redis_client

    def to_public(self, user: UserRow) -> UserPublic:
        """публичные данные сотрудника"""

        return UserPublic(
            id=user.id,
            login=user.login,
            role=user.role,
            created_at=user.created_at,
        )

    async def login(
        self, login: str, password: str, client_ip: str
    ) -> tuple[UserPublic, str]:
        """войти: проверить пароль, создать сессию, вернуть пользователя и токен"""

        # блокировка после слишком большого числа неудачных попыток
        key = self._failures_key(client_ip)
        failures = await self._redis_client.get(key)
        if failures is not None and int(failures) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="login blocked, try again later")

        user = await self._repository.get_user_by_login(login)

        if user is None or not verify_password(password, user.password_hash):
            # срок блокировки задаётся только при первой ошибке
            await self._redis_client.eval(
                RECORD_FAILED_LOGIN_SCRIPT,
                1,
                key,
                LOGIN_BLOCK_TTL_SECONDS,
            )
            raise HTTPException(status_code=401, detail="invalid login or password")

        # после успешного входа сбрасываем счётчик неудачных попыток
        await self._redis_client.delete(key)

        token = generate_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS)
        await self._repository.create_session(hash_token(token), user.id, expires_at)

        return self.to_public(user), token

    async def logout(self, token: str) -> None:
        """выйти: удалить сессию"""

        await self._repository.delete_session(hash_token(token))

    @staticmethod
    def _failures_key(client_ip: str) -> str:
        """ключ redis для счётчика неудачных входов с ip"""
        return f"auth:failed_login:{client_ip}"
