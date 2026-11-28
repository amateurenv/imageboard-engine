"""административные маршруты для настроек сайта"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import OwnerUserDependency, StaffServiceDependency, require_csrf
from app.core.site_settings import SiteSettings

router = APIRouter(
    prefix="/admin/settings",
    tags=["admin-settings"],
)

ADMIN_THEMES = {"dark", "light"}
ADMIN_COLORS = {"--accent-color", "--surface-color", "--text-color"}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def validate_appearance(settings: SiteSettings, current: SiteSettings) -> None:
    """проверить изменённые значения оформления и сохранить старые настройки"""

    appearance = settings.appearance
    previous = current.appearance
    if (
        appearance.active_theme != previous.active_theme
        and appearance.active_theme not in ADMIN_THEMES
    ):
        raise HTTPException(status_code=422, detail="invalid_appearance_theme")

    for name, value in appearance.style_vars.items():
        if value == previous.style_vars.get(name):
            continue
        if name not in ADMIN_COLORS or HEX_COLOR.fullmatch(value) is None:
            raise HTTPException(status_code=422, detail="invalid_appearance_color")

    if any(
        getattr(appearance, field) != getattr(previous, field)
        for field in ("logo_path", "favicon_path", "background_path")
    ):
        raise HTTPException(status_code=422, detail="appearance_paths_read_only")


@router.get("", response_model=SiteSettings)
async def get_settings(
    request: Request,
    owner_user: OwnerUserDependency,
) -> SiteSettings:
    """текущие настройки сайта"""

    return request.app.state.site_settings


@router.put("", response_model=SiteSettings, dependencies=[Depends(require_csrf)])
async def update_settings(
    request: Request,
    settings: SiteSettings,
    staff_service: StaffServiceDependency,
    owner_user: OwnerUserDependency,
) -> SiteSettings:
    """сохранить настройки в базу и обновить память приложения"""

    validate_appearance(settings, request.app.state.site_settings)
    await staff_service.save_settings(settings)

    # в памяти приложения обновляем проверенные настройки
    request.app.state.site_settings = settings

    return settings
