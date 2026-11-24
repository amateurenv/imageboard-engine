"""административные маршруты для тредов"""

from fastapi import APIRouter, HTTPException

from app.api.dependencies import StaffServiceDependency, StaffUserDependency
from app.schemas.staff import ThreadUpdateAdmin
from app.schemas.threads import ThreadDetail

router = APIRouter(
    prefix="/admin/threads",
    tags=["admin-threads"],
)


@router.patch("/{thread_id}", response_model=ThreadDetail)
async def update_thread(
    thread_id: int,
    update: ThreadUpdateAdmin,
    staff_service: StaffServiceDependency,
    staff_user: StaffUserDependency,
) -> ThreadDetail | None:
    """изменить тред из админки"""

    updated = await staff_service.update_thread(thread_id, update)
    if updated is None:
        raise HTTPException(status_code=404, detail="thread not found")

    return updated


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: int,
    staff_service: StaffServiceDependency,
    staff_user: StaffUserDependency,
) -> None:
    """удалить тред и все его посты"""

    await staff_service.delete_thread(staff_user.id, thread_id)
