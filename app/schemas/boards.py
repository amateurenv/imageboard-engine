"""формы ответов для досок"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoardMediaOverrides(BaseModel):
    """лимиты медиа конкретной доски, null берёт значение сайта"""

    model_config = ConfigDict(extra="forbid")

    # значения выше telegram-safe пределов покажут предупреждение в админке
    max_attachments_per_post: int | None = Field(default=None, ge=1, le=20)
    max_attachment_size_mib: int | None = Field(default=None, ge=1, le=100)
    max_total_size_mib: int | None = Field(default=None, ge=1, le=500)
    uploads_enabled: bool | None = None
    allow_anonymous_uploads: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_size_keys(cls, value: object) -> object:
        """превратить старые раздельные лимиты в единый без потери строгости"""

        if not isinstance(value, dict):
            return value
        values = dict(value)
        if "max_attachment_size_mib" not in values:
            legacy_sizes = [
                values.get("max_photo_size_mib"),
                values.get("max_other_media_size_mib"),
            ]
            numbers = [size for size in legacy_sizes if isinstance(size, int)]
            if numbers:
                values["max_attachment_size_mib"] = min(numbers)
        values.pop("max_photo_size_mib", None)
        values.pop("max_other_media_size_mib", None)
        return values

    @property
    def max_photo_size_mib(self) -> int | None:
        """оставить совместимость старого клиента с единым лимитом"""

        return self.max_attachment_size_mib

    @property
    def max_other_media_size_mib(self) -> int | None:
        """оставить совместимость старого клиента с единым лимитом"""

        return self.max_attachment_size_mib


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

    # треды в архиве не считаются активными, но сами записи из базы не пропадают
    thread_count: int

    # сюда входят и старые посты - это общий размер истории доски
    post_count: int

    # свежие посты за окно из settings.statistics, по умолчанию за сутки
    active_post_count: int

    # фронтенд не собирает URL из технических id или display name
    canonical_url: str = ""


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

    # пустой объект означает использовать общие telegram-safe лимиты
    media_overrides: BoardMediaOverrides = Field(default_factory=BoardMediaOverrides)


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

    # передай пустой объект, чтобы вернуть все лимиты к общим значениям сайта
    media_overrides: BoardMediaOverrides | None = None


class BoardManagement(BaseModel):
    """данные доски для админки"""

    slug: str
    title: str
    description: str
    position: int
    is_active: bool
    is_nsfw: bool
    media_overrides: BoardMediaOverrides
    # админка показывает это до сохранения слишком тяжёлого режима без Telegram
    telegram_sync_warning: str | None = None
