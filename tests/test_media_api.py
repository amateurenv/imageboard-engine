"""HTTP-проверки image upload без test postgresql и redis"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import (
    get_ip_bans_repository,
    get_media_upload_service,
    get_optional_current_user,
    get_spam_service,
)
from app.main import app
from app.repositories.media import MediaRecord
from app.services.media_queue import MediaProcessingState


class FakeIpBansRepository:
    """не блокировать тестовый запрос по IP"""

    async def get_by_ip(self, ip: str) -> None:
        """вернуть отсутствие бана"""

        return None


class FakeSpamService:
    """запомнить вызов rate limit до настоящей загрузки"""

    async def check_media_upload_limit(self, ip: str, slug: str) -> None:
        """разрешить одну тестовую загрузку"""


class RateLimitedSpamService:
    """воспроизвести ответ redis limiter без redis"""

    async def check_media_upload_limit(self, ip: str, slug: str) -> None:
        """вернуть обычный limiter error, который route нормализует"""

        raise HTTPException(status_code=429, detail="media upload rate limit exceeded")


class FakeMediaUploadService:
    """не писать файл, а проверить HTTP форму и закрытый ответ"""

    async def upload(self, **kwargs: object) -> MediaRecord:
        """вернуть queued запись без path в response schema"""

        assert kwargs["board_slug"] == "b"
        assert kwargs["token"] == "claim-token"
        return MediaRecord(
            id=7,
            path="quarantine/private.image",
            size=123,
            mime_type="image/jpeg",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            processing_state=MediaProcessingState.QUEUED,
            processing_attempts=0,
            processing_lease_until=None,
            last_processing_error=None,
        )


@pytest.mark.anyio
async def test_multipart_upload_returns_no_internal_storage_path() -> None:
    """route принимает form claim и не раскрывает quarantine path"""

    app.dependency_overrides = {
        get_ip_bans_repository: FakeIpBansRepository,
        get_spam_service: FakeSpamService,
        get_media_upload_service: FakeMediaUploadService,
        get_optional_current_user: lambda: None,
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/boards/b/media",
                data={"posting_claim_token": "claim-token"},
                files={"file": ("not-trusted.jpg", b"fake", "text/plain")},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json() == {
        "id": 7,
        "size": 123,
        "mime_type": "image/jpeg",
        "processing_state": "queued",
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.mark.anyio
async def test_multipart_upload_normalizes_rate_limit_code() -> None:
    """redis limiter не возвращает клиенту нестабильный технический текст"""

    app.dependency_overrides = {
        get_ip_bans_repository: FakeIpBansRepository,
        get_spam_service: RateLimitedSpamService,
        get_media_upload_service: FakeMediaUploadService,
        get_optional_current_user: lambda: None,
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/boards/b/media",
                data={"posting_claim_token": "claim-token"},
                files={"file": ("image.jpg", b"fake", "image/jpeg")},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.json() == {"detail": {"code": "media_rate_limited"}}
