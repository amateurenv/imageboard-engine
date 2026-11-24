"""административный маршрут для удаления постов"""

from fastapi import APIRouter

from app.api.dependencies import StaffServiceDependency, StaffUserDependency

router = APIRouter(
    prefix="/admin/posts",
    tags=["admin-posts"],
)


@router.delete("/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    staff_service: StaffServiceDependency,
    staff_user: StaffUserDependency,
) -> None:
    """удалить пост"""

    await staff_service.delete_post(staff_user.id, post_id)
