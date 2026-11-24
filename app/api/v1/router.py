"""маршруты первой версии публичного API"""

from fastapi import APIRouter

from app.api.v1.boards import router as boards_router
from app.api.v1.threads import router as threads_router

# все пользовательские маршруты первой версии начинаются с /api/v1
router = APIRouter(prefix="/api/v1")

# добавляем маршруты досок и тредов к первой версии api
router.include_router(boards_router)
router.include_router(threads_router)