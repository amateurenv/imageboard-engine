"""правила работы с публичным списком досок"""

from typing import Protocol

from app.repositories.boards import BoardRecord
from app.schemas.boards import BoardListResponse, BoardSummary


class BoardsReader(Protocol):
    """объект, который умеет читать активные доски"""

    async def list_active_boards(self) -> list[BoardRecord]:
        """вернуть активные доски"""


class BoardsService:
    """подготовка досок для публичного API"""

    def __init__(self, boards_repository: BoardsReader) -> None:
        # service зависит от нужного метода, а не от конкретной реализации
        self._boards_repository = boards_repository

    async def list_public_boards(self) -> BoardListResponse:
        """вернуть активные доски в безопасной форме API"""

        # repository получает данные из postgresql
        boards = await self._boards_repository.list_active_boards()

        # API получает только явно разрешённые публичные поля
        items = [
            BoardSummary(
                slug=board.slug,
                title=board.title,
                description=board.description,
                is_nsfw=board.is_nsfw,
            )
            for board in boards
        ]

        return BoardListResponse(items=items)