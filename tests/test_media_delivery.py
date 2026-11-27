"""проверка opaque media delivery без файловой раздачи FastAPI"""

from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_media_delivery_service, get_optional_current_user
from app.main import app
from app.repositories.media_delivery import DeliverableMedia, MediaDeliveryRepository
from app.services.media_delivery import MediaDelivery, MediaDeliveryService

PUBLIC_KEY = "qkY2CGr9Fa1r20YbDzoHcP6xE-kJe7T4dW8uVnMlAq0"


class FakeDeliveryRepository:
    """вернуть одну уже проверенную ready запись без postgres"""

    async def get_deliverable(self, public_key: str) -> DeliverableMedia | None:
        """не выдавать запись для неизвестного opaque key"""

        if public_key != PUBLIC_KEY:
            return None
        return DeliverableMedia(
            id=42,
            public_key=PUBLIC_KEY,
            mime_type="image/jpeg",
            display_path="display/42.webp",
            preview_path="previews/42.webp",
            board_type="public",
        )


@pytest.mark.anyio
async def test_delivery_service_builds_only_expected_internal_path() -> None:
    """id или storage path не могут попасть в X-Accel-Redirect из запроса"""

    service = MediaDeliveryService(cast(MediaDeliveryRepository, FakeDeliveryRepository()))

    display = await service.resolve(PUBLIC_KEY, "display", None)
    preview = await service.resolve(PUBLIC_KEY, "preview", None)

    assert display == MediaDelivery("image/webp", "/_imageboard_media/display/42.webp")
    assert preview == MediaDelivery("image/webp", "/_imageboard_media/previews/42.webp")
    assert await service.resolve("42", "display", None) is None
    assert await service.resolve(PUBLIC_KEY, "../../quarantine", None) is None


@pytest.mark.anyio
async def test_media_endpoint_returns_accel_headers_and_hides_unknown_key() -> None:
    """HTTP endpoint отдаёт nginx заголовок, но не byte stream или путь storage"""

    service = MediaDeliveryService(cast(MediaDeliveryRepository, FakeDeliveryRepository()))
    app.dependency_overrides = {
        get_media_delivery_service: lambda: service,
        get_optional_current_user: lambda: None,
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/media/{PUBLIC_KEY}/display")
            head = await client.head(f"/media/{PUBLIC_KEY}/preview")
            unknown = await client.get("/media/42/display")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-accel-redirect"] == "/_imageboard_media/display/42.webp"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "quarantine" not in str(response.headers)
    assert head.status_code == 200
    assert head.headers["x-accel-redirect"] == "/_imageboard_media/previews/42.webp"
    assert unknown.status_code == 404


def test_nginx_template_keeps_derivatives_internal() -> None:
    """template не создаёт public alias для quarantine или storage root"""

    config = open("deploy/nginx.conf.example", encoding="utf-8").read()

    assert "internal;" in config
    assert "autoindex off;" in config
    assert "display|previews" in config
    assert "default_type image/webp;" in config
    assert 'X-Content-Type-Options "nosniff"' in config
    assert "quarantine" in config
    assert "location /media/" not in config
