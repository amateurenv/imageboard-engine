"""административный маршрут для удаления постов"""

from fastapi import APIRouter

from app.api.dependencies import ModeratorUserDependency, StaffServiceDependency

router = APIRouter(
    prefix="/admin/posts",
    tags=["admin-posts"],
)


@router.delete("/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    staff_service: StaffServiceDependency,
    moderator_user: ModeratorUserDependency,
) -> None:
    """удалить пост"""

    await staff_service.delete_post(moderator_user.id, post_id)
