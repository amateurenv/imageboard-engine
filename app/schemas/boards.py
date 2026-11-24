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


class BoardCreate(BaseModel):
    """создать новую доску (данные из запроса)"""

    # то же правило формата, что и у BoardSummary
    slug: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )

    # название доски, которое увидит пользователь
    title: str = Field(
        min_length=1,
        max_length=1000,
    )

    # описание доски, по умолчанию пустая строка
    description: str = Field(default="", max_length=10000)

    # позиция доски в публичном списке
    position: int = Field(default=0)

    # доска активна и открыта для новых постов
    is_active: bool = True

    # доска помечена 18+
    is_nsfw: bool = False


class BoardUpdate(BaseModel):
    """изменить доску (данные из запроса, пустые — не менять)"""

    # новое название, пустая строка не допустима
    title: str | None = Field(default=None, min_length=1, max_length=1000)

    # новое описание
    description: str | None = Field(default=None, max_length=10000)

    # новая позиция в списке
    position: int | None = None

    # активна ли доска
    is_active: bool | None = None

    # отметка 18+
    is_nsfw: bool | None = None


class BoardManagement(BaseModel):
    """данные доски для админки"""

    slug: str
    title: str
    description: str
    position: int
    is_active: bool
    is_nsfw: bool