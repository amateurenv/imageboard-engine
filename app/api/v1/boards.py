"""публичные маршруты для досок"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import PostgresPoolDependency
from app.repositories.boards import BoardsRepository
from app.schemas.boards import BoardListResponse
from app.services.boards import BoardsService

# все маршруты этого модуля начинаются с /boards
router = APIRouter(
    prefix="/boards",
    tags=["boards"],
)

def get_boards_service(
    postgres_pool: PostgresPoolDependency,
) -> BoardsService:
    """создать service для одного HTTP-запроса"""

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