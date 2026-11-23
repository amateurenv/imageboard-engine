"""проверка HTTP-маршрута списка досок"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.boards import get_boards_service
from app.main import app
from app.schemas.boards import BoardListResponse, BoardSummary


class FakeBoardsService:
    """временная версия service для HTTP-теста"""

    # noinspection PyMethodMayBeStatic
    async def list_public_boards(self) -> BoardListResponse:
        """вернуть подготовленный ответ без postgresql"""

        return BoardListResponse(
            items=[
                BoardSummary(
                    slug="b",
                    title="Бред",
                    description="общение обо всём",
                    is_nsfw=False,
                )
            ]
        )

def get_fake_boards_service() -> FakeBoardsService:
    """вернуть service для тестового маршрута"""

    return FakeBoardsService()


@pytest.mark.anyio
async def test_list_boards_returns_public_data() -> None:
    """GET /api/v1/boards возвращает публичный список досок"""

    # временно заменяем настоящую зависимость тестовой версией
    app.dependency_overrides[get_boards_service] = get_fake_boards_service

    try:
        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/boards")
    finally:
        # после теста возвращаем приложению настоящую зависимость
        app.dependency_overrides.pop(get_boards_service, None)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "slug": "b",
                "title": "Бред",
                "description": "общение обо всём",
                "is_nsfw": False,
            }
        ]
    }