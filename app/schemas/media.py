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
