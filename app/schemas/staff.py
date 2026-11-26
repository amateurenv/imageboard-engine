"""формы запросов и ответов для админки"""

from datetime import datetime

from pydantic import BaseModel, Field, IPvAnyAddress


class ThreadUpdateAdmin(BaseModel):
    """изменить тред из админки (пустые поля — не менять)"""

    subject: str | None = Field(default=None, max_length=1000)
    is_sticky: bool | None = None
    is_locked: bool | None = None
    is_archived: bool | None = None


class IpBanCreate(BaseModel):
    """создать бан по IP"""

    # pydantic не пропустит строку, которая не является ip адресом
    ip: IPvAnyAddress
    reason: str | None = Field(default=None, max_length=500)


class IpBanPublic(BaseModel):
    """данные бана по IP"""

    ip: str
    reason: str | None
    created_at: datetime
