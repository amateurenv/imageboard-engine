"""маршруты для тредов"""

from fastapi import APIRouter, HTTPException, Request

from app.api.dependencies import (
    IpBansRepositoryDependency,
    ThreadsServiceDependency,
    check_ip_bans,
)
from app.schemas.threads import PostCreate, PostSummary, ThreadWithPosts

router = APIRouter(
    prefix="/threads",
    tags=["threads"],
)


@router.get("/{thread_id}", response_model=ThreadWithPosts)
async def get_thread(
    thread_id: int,
    threads_service: ThreadsServiceDependency,
) -> ThreadWithPosts | None:
    """тред и все его посты по порядку"""

    thread = await threads_service.get_thread(thread_id)
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
) -> PostSummary | None:
    """создать пост в треде"""

    # забаненные IP не могут создавать посты
    await check_ip_bans(request, ip_bans_repository)

    try:
        post = await threads_service.create_post(thread_id, post_create)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if post is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return post
