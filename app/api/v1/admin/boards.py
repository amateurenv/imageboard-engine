"""административные маршруты для досок"""

from fastapi import APIRouter, HTTPException

from app.api.dependencies import (
    AdminUserDependency,
    ModeratorUserDependency,
    StaffServiceDependency,
)
from app.schemas.boards import BoardCreate, BoardManagement, BoardUpdate

router = APIRouter(
    prefix="/admin/boards",
    tags=["admin-boards"],
)


@router.get("", response_model=list[BoardManagement])
async def list_boards(
    staff_service: StaffServiceDependency,
    moderator_user: ModeratorUserDependency,
) -> list[BoardManagement]:
    """все доски с полными данными (включая неактивные)"""

    return await staff_service.list_boards()


@router.post("", response_model=BoardManagement)
async def create_board(
    board_create: BoardCreate,
    staff_service: StaffServiceDependency,
    admin_user: AdminUserDependency,
) -> BoardManagement:
    """создать доску"""

    return await staff_service.create_board(board_create)


@router.patch("/{slug}", response_model=BoardManagement)
async def update_board(
    slug: str,
    board_update: BoardUpdate,
    staff_service: StaffServiceDependency,
    admin_user: AdminUserDependency,
) -> BoardManagement | None:
    """изменить доску"""

    updated = await staff_service.update_board(slug, board_update)
    if updated is None:
        raise HTTPException(status_code=404, detail="board not found")

    return updated


@router.delete("/{slug}", status_code=204)
async def delete_board(
    slug: str,
    staff_service: StaffServiceDependency,
    admin_user: AdminUserDependency,
) -> None:
    """удалить доску со всеми тредами"""

    if not await staff_service.delete_board(admin_user.id, slug):
        raise HTTPException(status_code=404, detail="board not found")
