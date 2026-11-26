"""проверка rate limit и определения IP клиента"""

from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import get_client_ip
from app.core.site_settings import SiteSettings
from app.services.spam import CHECK_AND_INCREMENT_SCRIPT, SpamService


class FakeRedis:
    """маленькая версия redis для проверки вызова lua-скрипта"""

    def __init__(self, results: list[int]) -> None:
        self._results = deque(results)
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> int:
        """сохранить аргументы и вернуть подготовленный результат"""

        self.calls.append(args)

        return self._results.popleft()


def make_request(client_ip: str, headers: dict[str, str] | None = None) -> Request:
    """создать минимальный HTTP-запрос для теста IP"""

    raw_headers = [
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": raw_headers,
            "client": (client_ip, 12345),
        }
    )


@pytest.mark.anyio
async def test_thread_limit_uses_client_ip_and_site_setting() -> None:
    """лимит тредов разделён между разными IP"""

    redis_client = FakeRedis([1])
    settings = SiteSettings.model_validate(
        {"thread": {"max_threads_per_board_day": 7}}
    )
    service = SpamService(redis_client, settings)

    await service.check_thread_limit("203.0.113.10", "b")

    assert redis_client.calls == [
        (
            CHECK_AND_INCREMENT_SCRIPT,
            1,
            "spam:threads:b:203.0.113.10",
            86400,
            7,
        )
    ]


@pytest.mark.anyio
async def test_post_limit_returns_429_when_redis_rejects_request() -> None:
    """redis отклоняет запрос после достижения лимита"""

    service = SpamService(FakeRedis([0]), SiteSettings())

    with pytest.raises(HTTPException) as error:
        await service.check_post_limit("203.0.113.10", 42)

    assert error.value.status_code == 429


@pytest.mark.anyio
async def test_zero_limit_disables_rate_limit() -> None:
    """значение 0 не делает запрос к redis"""

    redis_client = FakeRedis([])
    settings = SiteSettings.model_validate(
        {"post": {"max_posts_per_thread_hour": 0}}
    )
    service = SpamService(redis_client, settings)

    await service.check_post_limit("203.0.113.10", 42)

    assert redis_client.calls == []


def test_client_ip_ignores_untrusted_forwarded_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """прямой клиент не может подменить IP заголовком"""

    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: SimpleNamespace(trusted_proxy_ips="127.0.0.1"),
    )
    request = make_request(
        "203.0.113.10",
        {"x-forwarded-for": "198.51.100.20"},
    )

    assert get_client_ip(request) == "203.0.113.10"


def test_client_ip_uses_forwarded_header_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """доверенный proxy передаёт настоящий IP клиента"""

    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: SimpleNamespace(trusted_proxy_ips="127.0.0.1"),
    )
    request = make_request(
        "127.0.0.1",
        {"x-forwarded-for": "198.51.100.20, 127.0.0.1"},
    )

    assert get_client_ip(request) == "198.51.100.20"
