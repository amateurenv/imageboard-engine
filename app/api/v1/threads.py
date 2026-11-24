"""маршруты для тредов"""

from fastapi import APIRouter, HTTPException

from app.api.dependencies import ThreadsServiceDependency
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
    thread_id: int,
    post_create: PostCreate,
    threads_service: ThreadsServiceDependency,
) -> PostSummary | None:
    """создать пост в треде"""

    try:
        post = await threads_service.create_post(thread_id, post_create)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if post is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return post
