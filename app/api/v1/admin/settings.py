"""административные маршруты для настроек сайта"""

from fastapi import APIRouter, Request

from app.api.dependencies import StaffServiceDependency, StaffUserDependency
from app.core.site_settings import SiteSettings

router = APIRouter(
    prefix="/admin/settings",
    tags=["admin-settings"],
)


@router.get("", response_model=SiteSettings)
async def get_settings(request: Request) -> SiteSettings:
    """текущие настройки сайта"""

    return request.app.state.site_settings


@router.put("", response_model=SiteSettings)
async def update_settings(
    request: Request,
    settings: SiteSettings,
    staff_service: StaffServiceDependency,
    staff_user: StaffUserDependency,
) -> SiteSettings:
    """сохранить настройки в базу и обновить память приложения"""

    await staff_service.save_settings(settings)

    # в памяти приложения обновляем проверенные настройки
    request.app.state.site_settings = settings

    return settings
