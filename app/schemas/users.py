"""формы запросов и ответов для сотрудников"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=30)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, password: str) -> str:
        """не передавать bcrypt больше 72 байт"""

        if len(password.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return password


class RegisterSchema(BaseModel):
    """данные нового обычного пользователя без staff-роли"""

    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, password: str) -> str:
        """не передавать bcrypt больше 72 байт"""

        if len(password.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return password


class AccountPublic(BaseModel):
    """безопасные данные аккаунта для профиля и сессии"""

    id: int
    handle: str
    role: str
    created_at: datetime
    profile_url: str
