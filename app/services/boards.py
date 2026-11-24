"""правила работы с досками: чтение и управление"""

from dataclasses import asdict
from typing import Protocol

from app.repositories.boards import BoardDetailRecord, BoardRecord
from app.schemas.boards import (
    BoardCreate,
    BoardListResponse,
    BoardManagement,
    BoardSummary,
    BoardUpdate,
)


class BoardsReader(Protocol):
    """объект, который умеет читать и изменять доски"""

    async def list_active_boards(self) -> list[BoardRecord]:
        """вернуть активные доски"""

    async def get_by_slug(self, slug: str) -> BoardDetailRecord | None:
        """вернуть доску по slug"""

    async def create_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
    ) -> BoardDetailRecord:
        """создать доску"""

    async def update_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
    ) -> BoardDetailRecord | None:
        """изменить доску"""


class BoardsService:
    """подготовка досок для публичного api и управление ими"""

    def __init__(self, boards_repository: BoardsReader) -> None:
        # service зависит от нужного метода, а не от конкретной реализации
        self._boards_repository = boards_repository

    async def list_public_boards(self) -> BoardListResponse:
        """вернуть активные доски в безопасной форме api"""

        # repository получает данные из postgresql
        boards = await self._boards_repository.list_active_boards()

        # api получает только явно разрешённые публичные поля
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

    async def get_public_board(self, slug: str) -> BoardSummary | None:
        """вернуть публичные поля одной доски по slug"""

        board = await self._boards_repository.get_by_slug(slug)
        if board is None:
            return None

        return BoardSummary(
            slug=board.slug,
            title=board.title,
            description=board.description,
            is_nsfw=board.is_nsfw,
        )

    async def create_board(self, board_create: BoardCreate) -> BoardManagement:
        """создать доску и вернуть её данные"""

        return await self._boards_repository.create_board(
            slug=board_create.slug,
            title=board_create.title,
            description=board_create.description,
            position=board_create.position,
            is_active=board_create.is_active,
            is_nsfw=board_create.is_nsfw,
        )

    async def update_board(self, slug: str, update: BoardUpdate) -> BoardManagement | None:
        """изменить доску, None если такая доска не найдена"""

        current = await self._boards_repository.get_by_slug(slug)
        if current is None:
            return None

        # None в запросе значит «не менять», поэтому мержим с текущими значениями
        values = asdict(current)
        values.update(update.model_dump(exclude_none=True))

        return await self._boards_repository.update_board(**values)