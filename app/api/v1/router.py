"""маршруты первой версии публичного API"""

from fastapi import APIRouter

# все пользовательские маршруты первой версии будут начинаться с /api/v1
router = APIRouter(prefix="/api/v1")