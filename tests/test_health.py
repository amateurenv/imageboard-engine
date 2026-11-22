"""Минимальные автоматические проверки ASGI-приложения."""

# Сторонние библиотеки: pytest запускает тест, ASGITransport направляет запросы прямо
# в FastAPI, а AsyncClient ведёт себя как асинхронный HTTP-клиент.
import pytest
from httpx import ASGITransport, AsyncClient

# Импортируем созданное приложение, которое будем тестировать.
from app.main import app


# Этот декоратор говорит pytest, что async-тест надо запустить через библиотеку AnyIO.
@pytest.mark.anyio
# async def позволяет использовать await внутри теста.
# ``-> None`` означает, что тест не возвращает полезного значения, а только проверяет условия.
async def test_healthcheck_returns_application_status() -> None:
    """Маршрут /health должен вернуть предсказуемый статус приложения."""

    # Transport перенаправляет запросы HTTPX в объект app, не открывая TCP-порт.
    transport = ASGITransport(app=app)
    # async with гарантированно закрывает клиента после блока, даже если тест упадёт.
    # base_url — формальный адрес, необходимый HTTPX для сборки URL запроса.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # await ждёт асинхронного ответа, не блокируя цикл событий.
        response = await client.get("/health")

    # assert останавливает тест с ошибкой, если HTTP-статус отличается от успешного 200.
    assert response.status_code == 200
    # response.json() превращает тело ответа из JSON в словарь Python и сравнивает его целиком.
    assert response.json() == {"status": "ok", "environment": "development"}
