"""формы запросов и ответов для медиа"""

from datetime import datetime

from pydantic import BaseModel, Field


class MediaSummary(BaseModel):
    """данные загруженного файла"""

    id: int
    path: str
    size: int = Field(ge=0)
    mime_type: str
    created_at: datetime


class MediaUploadResponse(BaseModel):
    """безопасный ответ upload без внутренних путей storage"""

    id: int
    size: int = Field(ge=0)
    mime_type: str
    processing_state: str
    created_at: datetime


class PublicMediaUrls(BaseModel):
    """готовые ссылки на derivative без внутреннего id или пути storage"""

    preview_url: str
    display_url: str
    mime_type: str = "image/webp"
    # true означает animated WebP display, preview всегда остаётся статичным WebP
    is_animation: bool = False
