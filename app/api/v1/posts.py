"""self-delete маршруты ответов без staff или profile owner прав"""

from fastapi import APIRouter, HTTPException, Request

from app.api.dependencies import (
    IpBansRepositoryDependency,
    OptionalCurrentUserDependency,
    SelfDeleteServiceDependency,
    SpamServiceDependency,
    check_ip_bans,
    get_client_ip,
    require_csrf,
)
from app.schemas.threads import SelfDeleteRequest
from app.services.self_delete import SelfDeleteError

router = APIRouter(prefix="/posts", tags=["posts"])


@router.delete("/{post_id}", status_code=204)
async def self_delete_post(
    request: Request,
    post_id: int,
    deletion: SelfDeleteRequest,
    service: SelfDeleteServiceDependency,
    ip_bans_repository: IpBansRepositoryDependency,
    spam_service: SpamServiceDependency,
    current_user: OptionalCurrentUserDependency,
) -> None:
    """удалить свой не-op ответ по session или анонимному deletion password"""

    await check_ip_bans(request, ip_bans_repository)
    if current_user is not None:
        await require_csrf(request)
    elif deletion.deletion_password is not None:
        try:
            await spam_service.check_deletion_password_limit(get_client_ip(request), post_id)
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            raise HTTPException(
                status_code=429,
                detail={"code": "deletion_password_rate_limited"},
            ) from exc
    try:
        deleted = await service.delete_post(post_id, current_user, deletion.deletion_password)
    except SelfDeleteError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="post not found")
