"""публичные маршруты для досок"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import PostgresPoolDependency
from app.repositories.boards import BoardsRepository
from app.schemas.boards import (
    BoardCreate,
    BoardListResponse,
    BoardManagement,
    BoardSummary,
    BoardUpdate,
)
from app.services.boards import BoardsService

# все маршруты этого модуля начинаются с /boards
router = APIRouter(
    prefix="/boards",
    tags=["boards"],
)

def get_boards_service(
    postgres_pool: PostgresPoolDependency,
) -> BoardsService:
    """создать service для одного http-запроса"""

    # эти объекты лёгкие и не создают новое подключение к postgresql
    repository = BoardsRepository(postgres_pool)

    return BoardsService(repository)

# fastapi вызовет get_boards_service и передаст результат в маршрут
type BoardsServiceDependency = Annotated[
    BoardsService,
    Depends(get_boards_service),
]

@router.get("", response_model=BoardListResponse)
async def list_boards(
    boards_service: BoardsServiceDependency,
) -> BoardListResponse:
    """вернуть список активных досок"""

    return await boards_service.list_public_boards()


@router.get("/{slug}", response_model=BoardSummary)
async def get_board(
    slug: str,
    boards_service: BoardsServiceDependency,
) -> BoardSummary | None:
    """вернуть доску по slug"""

    board = await boards_service.get_public_board(slug)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")

    return board


@router.post("", response_model=BoardManagement)
async def create_board(
    board_create: BoardCreate,
    boards_service: BoardsServiceDependency,
) -> BoardManagement:
    """создать доску"""

    return await boards_service.create_board(board_create)


@router.patch("/{slug}", response_model=BoardManagement)
async def update_board(
    slug: str,
    board_update: BoardUpdate,
    boards_service: BoardsServiceDependency,
) -> BoardManagement | None:
    """изменить доску"""

    updated = await boards_service.update_board(slug, board_update)
    if updated is None:
        raise HTTPException(status_code=404, detail="board not found")

    return updated