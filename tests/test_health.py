"""минимальная проверка asgi приложения"""

# pytest запускает тест, httpx отправляет запрос прямо в fastapi
import pytest
from httpx import ASGITransport, AsyncClient

# приложение для проверки
from app.main import app


# pytest запустит async тест через anyio
@pytest.mark.anyio
# async def позволяет использовать await
async def test_healthcheck_returns_application_status() -> None:
    """маршрут /health возвращает ожидаемый ответ"""

    # запрос идёт в app без настоящего сетевого порта
    transport = ASGITransport(app=app)
    # клиент закроется после блока
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ждём ответ без блокировки event loop
        response = await client.get("/health")

    # успешный http статус
    assert response.status_code == 200
    # тело ответа совпадает с ожидаемым json
    assert response.json() == {"status": "ok", "environment": "development"}
