"""контракты личных досок без привязки к html или Telegram"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BoardType = Literal["public", "profile"]
PostingPolicy = Literal["owner_only", "authenticated", "anyone"]


class ProfileBoard(BaseModel):
    """публичное представление личной доски"""

    handle: str
    name: str
    description: str
    rules: str
    thread_creation_policy: PostingPolicy
    reply_policy: PostingPolicy
    canonical_url: str
    created_at: datetime
    updated_at: datetime


class ProfileBoardUpdate(BaseModel):
    """изменяемые владельцем поля его личной доски"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=10000)
    rules: str | None = Field(default=None, max_length=10000)
    thread_creation_policy: PostingPolicy | None = None
    reply_policy: PostingPolicy | None = None


class ProfileThreadModeration(BaseModel):
    """обратимые owner-действия над тредом без изменения текста"""

    model_config = ConfigDict(extra="forbid")

    is_sticky: bool | None = None
    is_locked: bool | None = None
    is_hidden: bool | None = None


class ProfilePostModeration(BaseModel):
    """обратимое скрытие ответа владельцем profile-доски"""

    model_config = ConfigDict(extra="forbid")

    is_hidden: bool


class PostingPermissionDetail(BaseModel):
    """стабильная ошибка policy для будущих клиентов"""

    code: Literal["authentication_required", "posting_forbidden"]
    message: str
