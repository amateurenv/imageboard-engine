"""формы запросов и ответов для сотрудников"""

from datetime import datetime

from pydantic import BaseModel, Field

# доступные роли сотрудников
STAFF_ROLES = ("owner", "admin", "moderator")


class UserPublic(BaseModel):
    """данные сотрудника для внешнего api"""

    id: int
    login: str
    role: str
    created_at: datetime


class LoginSchema(BaseModel):
    """данные для входа по login и паролю"""

    login: str = Field(min_length=1, max_length=30)
    password: str = Field(min_length=8, max_length=200)
