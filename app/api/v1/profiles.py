"""api личных досок-профилей без публичных html-страниц"""

# ruff: noqa: E501 - объявления маршрутов содержат длинные FastAPI-аннотации

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import (
    CurrentUserDependency,
    ProfileModerationServiceDependency,
    ProfilesServiceDependency,
    require_csrf,
)
from app.schemas.profiles import (
    ProfileBoard,
    ProfileBoardUpdate,
    ProfilePostModeration,
    ProfileThreadModeration,
)
from app.services.profile_moderation import ProfileModerationError
from app.services.profiles import ProfileAccessError

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("/{handle}", response_model=ProfileBoard)
async def get_profile(
    handle: str,
    profiles_service: ProfilesServiceDependency,
) -> ProfileBoard:
    """прочитать profile-доску по handle без символа @"""

    profile = await profiles_service.get_profile(handle)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


@router.patch("/{handle}/threads/{thread_id}", dependencies=[Depends(require_csrf)])
async def moderate_thread(handle: str, thread_id: int, update: ProfileThreadModeration, current_user: CurrentUserDependency, service: ProfileModerationServiceDependency) -> None:
    """изменить обратимое состояние треда своей profile-доски"""
    try:
        if not await service.update_thread(handle, thread_id, current_user, **update.model_dump(exclude_none=True)):
            raise HTTPException(status_code=404, detail="thread not found")
    except ProfileModerationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.patch("/{handle}/posts/{post_id}", dependencies=[Depends(require_csrf)])
async def moderate_post(handle: str, post_id: int, update: ProfilePostModeration, current_user: CurrentUserDependency, service: ProfileModerationServiceDependency) -> None:
    """скрыть или вернуть ответ своей profile-доски"""
    try:
        if not await service.update_post(handle, post_id, current_user, update.is_hidden):
            raise HTTPException(status_code=404, detail="post not found")
    except ProfileModerationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.delete("/{handle}/threads/{thread_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_profile_thread(handle: str, thread_id: int, current_user: CurrentUserDependency, service: ProfileModerationServiceDependency) -> None:
    """удалить тред profile-доски вместе с каскадом его данных"""
    try:
        if not await service.delete_thread(handle, thread_id, current_user):
            raise HTTPException(status_code=404, detail="thread not found")
    except ProfileModerationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.delete("/{handle}/posts/{post_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_profile_post(handle: str, post_id: int, current_user: CurrentUserDependency, service: ProfileModerationServiceDependency) -> None:
    """удалить не-OP ответ profile-доски"""
    try:
        if not await service.delete_post(handle, post_id, current_user):
            raise HTTPException(status_code=404, detail="post not found")
    except ProfileModerationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.patch("/{handle}", response_model=ProfileBoard, dependencies=[Depends(require_csrf)])
async def update_profile(
    handle: str,
    update: ProfileBoardUpdate,
    current_user: CurrentUserDependency,
    profiles_service: ProfilesServiceDependency,
) -> ProfileBoard:
    """изменить разрешённые поля своей profile-доски"""

    try:
        profile = await profiles_service.update_profile(
            handle=handle,
            actor=current_user,
            update=update,
        )
    except ProfileAccessError as error:
        raise HTTPException(
            status_code=403,
            detail={"code": "profile_forbidden", "message": str(error)},
        ) from error
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile
