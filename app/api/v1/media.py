"""маршрут для загрузки файлов медиа"""

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.api.dependencies import MediaServiceDependency
from app.schemas.media import MediaSummary

router = APIRouter(
    prefix="/media",
    tags=["media"],
)


@router.post("", response_model=MediaSummary)
async def upload_media(
    media_service: MediaServiceDependency,
    file: Annotated[UploadFile, File()],
) -> MediaSummary:
    """загрузить файл (картинки, видео, голосовые/файлы/музыка)"""

    return await media_service.upload(file)
