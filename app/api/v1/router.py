"""маршруты первой версии публичного api"""

from fastapi import APIRouter

from app.api.v1.admin.boards import router as admin_boards_router
from app.api.v1.admin.ip_bans import router as admin_ip_bans_router
from app.api.v1.admin.posts import router as admin_posts_router
from app.api.v1.admin.settings import router as admin_settings_router
from app.api.v1.admin.threads import router as admin_threads_router
from app.api.v1.auth import router as auth_router
from app.api.v1.boards import router as boards_router
from app.api.v1.media import router as media_router
from app.api.v1.threads import router as threads_router
from app.api.v1.ws import router as ws_router

# все пользовательские маршруты первой версии начинаются с /api/v1
router = APIRouter(prefix="/api/v1")

# добавляем маршруты авторизации
router.include_router(auth_router)
# добавляем маршруты загрузки медиа
router.include_router(media_router)
# добавляем маршруты досок и тредов к первой версии api
router.include_router(boards_router)
router.include_router(threads_router)
# добавляем websocket-маршрут для realtime-событий
router.include_router(ws_router)
# добавляем административные маршруты
router.include_router(admin_boards_router)
router.include_router(admin_threads_router)
router.include_router(admin_posts_router)
router.include_router(admin_settings_router)
router.include_router(admin_ip_bans_router)