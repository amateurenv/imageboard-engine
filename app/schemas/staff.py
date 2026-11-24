"""формы запросов и ответов для админки"""

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadUpdateAdmin(BaseModel):
    """изменить тред из админки (пустые поля — не менять)"""

    subject: str | None = Field(default=None, max_length=1000)
    is_sticky: bool | None = None
    is_locked: bool | None = None
    is_archived: bool | None = None


class IpBanCreate(BaseModel):
    """создать бан по IP"""

    ip: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


class IpBanPublic(BaseModel):
    """данные бана по IP"""

    ip: str
    reason: str | None
    created_at: datetime
