"""проверка защиты входа сотрудников"""

import pytest
from fastapi import HTTPException

from app.services.users import LOGIN_BLOCK_TTL_SECONDS, UsersService


class FakeUsersRepository:
    """репозиторий без найденных сотрудников"""

    # noinspection PyMethodMayBeStatic
    async def get_user_by_login(self, login: str) -> None:
        """вернуть отсутствие пользователя"""

        return None


class FakeRedis:
    """маленькая версия redis для счётчика ошибок"""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> int | None:
        """вернуть сохранённый счётчик"""

        return self.values.get(key)

    async def eval(
        self,
        script: str,
        number_of_keys: int,
        key: str,
        ttl: int,
    ) -> int:
        """повторить нужную часть lua скрипта"""

        assert number_of_keys == 1
        assert ttl == LOGIN_BLOCK_TTL_SECONDS

        failures = self.values.get(key, 0) + 1
        self.values[key] = failures
        if failures == 1:
            self.expire_calls.append((key, ttl))

        return failures


@pytest.mark.anyio
async def test_failed_login_sets_block_expiry_only_once() -> None:
    """каждая ошибка увеличивает счётчик, но не продлевает блокировку"""

    redis_client = FakeRedis()
    service = UsersService(FakeUsersRepository(), redis_client)  # type: ignore[arg-type]

    for _ in range(5):
        with pytest.raises(HTTPException) as error:
            await service.login("unknown", "wrong", "127.0.0.1")

        assert error.value.status_code == 401

    with pytest.raises(HTTPException) as error:
        await service.login("unknown", "wrong", "127.0.0.1")

    assert error.value.status_code == 429
    assert redis_client.expire_calls == [
        ("auth:failed_login:127.0.0.1", LOGIN_BLOCK_TTL_SECONDS)
    ]
