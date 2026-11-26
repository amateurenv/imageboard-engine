"""маршруты для тредов"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.dependencies import (
    IpBansRepositoryDependency,
    SpamServiceDependency,
    ThreadsServiceDependency,
    check_ip_bans,
    get_client_ip,
)
from app.core.pagination import decode_post_cursor
from app.schemas.threads import PostCreate, PostSummary, ThreadPageResponse

router = APIRouter(
    prefix="/threads",
    tags=["threads"],
)


@router.get("/{thread_id}", response_model=ThreadPageResponse)
async def get_thread(
    thread_id: int,
    threads_service: ThreadsServiceDependency,
    limit: int = Query(
        default=100,
        ge=1,
        le=100,
        description="число ответов на странице",
    ),
    cursor: str | None = Query(
        default=None,
        description="cursor из предыдущего ответа",
    ),
) -> ThreadPageResponse | None:
    """OP и страница последних или более старых ответов"""

    post_cursor = decode_post_cursor(cursor, thread_id)
    thread = await threads_service.get_thread(thread_id, post_cursor, limit)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return thread


@router.post("/{thread_id}/posts", response_model=PostSummary)
async def create_post(
    request: Request,
    thread_id: int,
    post_create: PostCreate,
    threads_service: ThreadsServiceDependency,
    ip_bans_repository: IpBansRepositoryDependency,
    spam_service: SpamServiceDependency,
) -> PostSummary | None:
    """создать пост в треде"""

    # забаненные IP не могут создавать посты
    await check_ip_bans(request, ip_bans_repository)

    # rate limit постинга в тред
    await spam_service.check_post_limit(get_client_ip(request), thread_id)

    try:
        post = await threads_service.create_post(thread_id, post_create)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if post is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return post
