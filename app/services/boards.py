"""правила работы с досками: чтение и управление"""

from dataclasses import asdict
from typing import Protocol

from app.core.site_settings import SiteSettings
from app.core.urls import board_canonical_url
from app.repositories.boards import BoardDetailRecord, BoardRecord
from app.schemas.boards import (
    BoardCreate,
    BoardListResponse,
    BoardManagement,
    BoardMediaOverrides,
    BoardSummary,
    BoardUpdate,
)


class BoardsReader(Protocol):
    """объект, который умеет читать и изменять доски"""

    async def list_active_boards(self, activity_window_hours: int) -> list[BoardRecord]:
        """вернуть активные доски"""

    async def get_by_slug(self, slug: str) -> BoardDetailRecord | None:
        """вернуть доску по slug"""

    async def get_active_board(
        self, slug: str, activity_window_hours: int
    ) -> BoardRecord | None:
        """вернуть публичную доску с её счётчиками"""

    async def create_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
        media_overrides: dict[str, object],
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
        media_overrides: dict[str, object],
    ) -> BoardDetailRecord | None:
        """изменить доску"""


class BoardsService:
    """подготовка досок для публичного api и управление ими"""

    def __init__(self, boards_repository: BoardsReader, site_settings: SiteSettings) -> None:
        # service зависит от нужного метода, а не от конкретной реализации
        self._boards_repository = boards_repository
        self._site_settings = site_settings

    async def list_public_boards(self) -> BoardListResponse:
        """вернуть активные доски в безопасной форме api"""

        # передаём часы, а не готовую дату: postgres посчитает окно по своему времени
        boards = await self._boards_repository.list_active_boards(
            self._site_settings.statistics.activity_window_hours
        )

        # собираем ответ явно, чтобы случайно не показать staff-поля из базы
        items = [
            BoardSummary(
                slug=board.slug,
                title=board.title,
                description=board.description,
                is_nsfw=board.is_nsfw,
                thread_count=board.thread_count,
                post_count=board.post_count,
                active_post_count=board.active_post_count,
                canonical_url=board_canonical_url("public", board.slug),
            )
            for board in boards
        ]

        return BoardListResponse(items=items)

    async def get_public_board(self, slug: str) -> BoardSummary | None:
        """вернуть публичные поля одной доски по slug"""

        board = await self._boards_repository.get_active_board(
            slug, self._site_settings.statistics.activity_window_hours
        )
        if board is None:
            return None

        return BoardSummary(
            slug=board.slug,
            title=board.title,
            description=board.description,
            is_nsfw=board.is_nsfw,
            thread_count=board.thread_count,
            post_count=board.post_count,
            active_post_count=board.active_post_count,
            canonical_url=board_canonical_url("public", board.slug),
        )

    async def create_board(self, board_create: BoardCreate) -> BoardManagement:
        """создать доску и вернуть её данные"""

        board = await self._boards_repository.create_board(
            slug=board_create.slug,
            title=board_create.title,
            description=board_create.description,
            position=board_create.position,
            is_active=board_create.is_active,
            is_nsfw=board_create.is_nsfw,
            media_overrides=board_create.media_overrides.model_dump(exclude_none=True),
        )

        return self._to_management(board)

    async def update_board(self, slug: str, update: BoardUpdate) -> BoardManagement | None:
        """изменить доску, None если такая доска не найдена"""

        current = await self._boards_repository.get_by_slug(slug)
        if current is None:
            return None

        # None в запросе значит «не менять», поэтому мержим с текущими значениями
        values = asdict(current)
        values.update(update.model_dump(exclude_none=True))

        updated = await self._boards_repository.update_board(**values)
        if updated is None:
            return None

        return self._to_management(updated)

    def _to_management(self, board: BoardDetailRecord) -> BoardManagement:
        """запись репозитория в схему ответа админки"""

        return BoardManagement(
            slug=board.slug,
            title=board.title,
            description=board.description,
            position=board.position,
            is_active=board.is_active,
            is_nsfw=board.is_nsfw,
            media_overrides=BoardMediaOverrides.model_validate(board.media_overrides),
        )
