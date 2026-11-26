"""публичные маршруты для досок"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.dependencies import (
    IpBansRepositoryDependency,
    PostgresPoolDependency,
    SpamServiceDependency,
    ThreadsServiceDependency,
    check_ip_bans,
    get_client_ip,
)
from app.core.pagination import decode_thread_cursor
from app.repositories.boards import BoardsRepository
from app.schemas.boards import BoardListResponse, BoardSummary
from app.schemas.threads import ThreadCreate, ThreadListResponse, ThreadWithPosts
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


@router.get("/{slug}/threads", response_model=ThreadListResponse)
async def list_threads(
    slug: str,
    threads_service: ThreadsServiceDependency,
    limit: int = Query(
        default=30,
        ge=1,
        le=100,
        description="число тредов на странице",
    ),
    cursor: str | None = Query(
        default=None,
        description="cursor из предыдущего ответа",
    ),
) -> ThreadListResponse | None:
    """страница тредов доски"""

    thread_cursor = decode_thread_cursor(cursor, slug)
    response = await threads_service.list_threads(slug, thread_cursor, limit)
    if response is None:
        raise HTTPException(status_code=404, detail="board not found")

    return response


@router.post("/{slug}/threads", response_model=ThreadWithPosts, status_code=201)
async def create_thread(
    request: Request,
    slug: str,
    thread_create: ThreadCreate,
    threads_service: ThreadsServiceDependency,
    ip_bans_repository: IpBansRepositoryDependency,
    spam_service: SpamServiceDependency,
) -> ThreadWithPosts | None:
    """создать тред с первым постом"""

    # забаненные IP не могут создавать треды
    await check_ip_bans(request, ip_bans_repository)

    # rate limit создания тредов на доску
    await spam_service.check_thread_limit(get_client_ip(request), slug)

    try:
        thread = await threads_service.create_thread(slug, thread_create)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if thread is None:
        raise HTTPException(status_code=404, detail="board not found")

    return thread
