"""формы ответов для досок"""

from pydantic import BaseModel, Field


class BoardSummary(BaseModel):
    """краткая публичная информация о доске"""

    # короткое имя доски для URL, например b или tech
    slug: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )

    # название, которое увидит пользователь
    title: str = Field(
        min_length=1,
        max_length=1000,
    )

    # краткое описание доски, пустая строка разрешена
    description: str = Field(
        max_length=10000,
    )

    # фронтенд сможет показать предупреждение до открытия доски
    is_nsfw: bool


class BoardListResponse(BaseModel):
    """ответ со списком доступных досок"""

    # default_factory создаёт новый пустой список для каждого ответа
    items: list[BoardSummary] = Field(default_factory=list)