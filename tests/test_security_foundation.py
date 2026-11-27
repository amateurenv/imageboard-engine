"""проверка базовых защитных настроек"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.dependencies import require_csrf
from app.core.config import Settings
from app.core.security import CSRF_COOKIE, CSRF_HEADER


def make_request(cookie: str | None, header: str | None) -> Request:
    """создать запрос с csrf-данными для теста"""

    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{CSRF_COOKIE}={cookie}".encode()))
    if header is not None:
        headers.append((CSRF_HEADER.encode(), header.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_production_rejects_debug() -> None:
    """production не принимает включённый debug"""

    with pytest.raises(ValidationError, match="APP_DEBUG"):
        Settings(
            app_env="production",
            app_debug=True,
            database_url="postgresql://user:password@localhost:5432/database",
            redis_url="redis://localhost:6379/0",
        )


@pytest.mark.anyio
async def test_csrf_requires_matching_cookie_and_header() -> None:
    """изменение staff-состояния требует совпадающий csrf-токен"""

    await require_csrf(make_request("token", "token"))
    with pytest.raises(HTTPException) as error:
        await require_csrf(make_request("token", "different"))
    assert error.value.status_code == 403
