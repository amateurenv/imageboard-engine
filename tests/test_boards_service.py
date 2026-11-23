"""проверка правил публичного списка досок"""

import pytest

from app.repositories.boards import BoardRecord
from app.services.boards import BoardsService


class FakeBoardsRepository:
    """временный источник досок для теста"""

    def __init__(self, boards: list[BoardRecord]) -> None:
        self._boards = boards

    async def list_active_boards(self) -> list[BoardRecord]:
        """вернуть заранее подготовленные доски"""

        return self._boards


@pytest.mark.anyio
async def test_list_public_boards_returns_active_boards() -> None:
    """service возвращает доски в порядке, который дал repository"""

    repository = FakeBoardsRepository(
        [
            BoardRecord(
                slug="b",
                title="Бред",
                description="общение обо всём",
                is_nsfw=False,
            ),
            BoardRecord(
                slug="adult",
                title="Только для взрослых",
                description="",
                is_nsfw=True,
            ),
        ]
    )
    service = BoardsService(repository)

    response = await service.list_public_boards()

    assert response.model_dump() == {
        "items": [
            {
                "slug": "b",
                "title": "Бред",
                "description": "общение обо всём",
                "is_nsfw": False,
            },
            {
                "slug": "adult",
                "title": "Только для взрослых",
                "description": "",
                "is_nsfw": True,
            },
        ]
    }


@pytest.mark.anyio
async def test_list_public_boards_returns_empty_list() -> None:
    """service возвращает пустой список, если активных досок нет"""

    service = BoardsService(FakeBoardsRepository([]))

    response = await service.list_public_boards()

    assert response.items == []