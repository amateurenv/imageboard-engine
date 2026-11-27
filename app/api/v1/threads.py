"""маршруты для тредов"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.dependencies import (
    IpBansRepositoryDependency,
    OptionalCurrentUserDependency,
    SelfDeleteServiceDependency,
    SpamServiceDependency,
    ThreadsServiceDependency,
    check_ip_bans,
    get_client_ip,
    require_csrf,
)
from app.core.pagination import decode_post_cursor
from app.schemas.threads import PostCreate, PostSummary, SelfDeleteRequest, ThreadPageResponse
from app.services.media_policy import MediaPolicyError
from app.services.posting_claims import PostingClaimError
from app.services.posting_policy import PostingPermissionError
from app.services.self_delete import SelfDeleteError
from app.services.threads import DeletionPasswordError

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
    current_user: OptionalCurrentUserDependency,
) -> PostSummary | None:
    """создать пост в треде"""

    # забаненные IP не могут создавать посты
    await check_ip_bans(request, ip_bans_repository)

    # rate limit постинга в тред
    await spam_service.check_post_limit(get_client_ip(request), thread_id)

    try:
        post = await threads_service.create_post(thread_id, post_create, current_user)
    except PostingPermissionError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code.value, "message": str(exc)},
        ) from exc
    except DeletionPasswordError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": "deletion password is required"},
        ) from exc
    except PostingClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    except MediaPolicyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_detail()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if post is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return post


@router.delete("/{thread_id}", status_code=204)
async def self_delete_thread(
    request: Request,
    thread_id: int,
    deletion: SelfDeleteRequest,
    service: SelfDeleteServiceDependency,
    ip_bans_repository: IpBansRepositoryDependency,
    spam_service: SpamServiceDependency,
    current_user: OptionalCurrentUserDependency,
) -> None:
    """удалить свой тред по session или анонимному deletion password"""

    await check_ip_bans(request, ip_bans_repository)
    if current_user is not None:
        await require_csrf(request)
    elif deletion.deletion_password is not None:
        try:
            await spam_service.check_deletion_password_limit(get_client_ip(request), thread_id)
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            raise HTTPException(
                status_code=429,
                detail={"code": "deletion_password_rate_limited"},
            ) from exc
    try:
        deleted = await service.delete_thread(thread_id, current_user, deletion.deletion_password)
    except SelfDeleteError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="thread not found")
