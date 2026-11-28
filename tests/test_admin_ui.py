"""проверка локальной страницы управления"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.anyio
async def test_admin_page_is_available_without_api_schema() -> None:
    """страница управления доступна, а её api остаётся отдельным"""

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "Управление" in response.text
    assert "csrf_token" in response.text
    assert "Создать доску" in response.text
    assert "Сохранить изменения" in response.text
    assert "Модерация" in response.text
    assert "IP-баны" in response.text
    assert "Предпросмотр оформления" in response.text
