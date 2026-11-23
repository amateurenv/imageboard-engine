"""маршруты первой версии публичного API"""

from fastapi import APIRouter

from app.api.v1.boards import router as boards_router

# все пользовательские маршруты первой версии начинаются с /api/v1
router = APIRouter(prefix="/api/v1")

# добавляем маршруты досок к первой версии API
router.include_router(boards_router)