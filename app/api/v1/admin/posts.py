"""административный маршрут для удаления постов"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import ModeratorUserDependency, StaffServiceDependency, require_csrf

router = APIRouter(
    prefix="/admin/posts",
    tags=["admin-posts"],
)


@router.delete("/{post_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_post(
    post_id: int,
    staff_service: StaffServiceDependency,
    moderator_user: ModeratorUserDependency,
) -> None:
    """удалить пост"""

    try:
        await staff_service.delete_post(moderator_user.id, post_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
