"""правила и значения по умолчанию для имиджборды"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


# настройки создания тредов
class ThreadSettings(BaseModel):
    """лимиты для тредов"""

    # неизвестные поля в настройках считаются ошибкой
    model_config = ConfigDict(extra="forbid")

    # 0 разрешает тред без заголовка
    max_subject_length: int = Field(default=200, ge=0, le=1000)

    # 0 выключает лимит новых тредов на одной доске за сутки
    max_threads_per_board_day: int = Field(default=3, ge=0, le=1000)


# настройки создания постов
class PostSettings(BaseModel):
    """лимиты для постов"""

    model_config = ConfigDict(extra="forbid")

    # лимит текста одного поста
    max_post_length: int = Field(default=10000, ge=1, le=100000)

    # 0 запрещает собственное имя автора
    max_author_name_length: int = Field(default=50, ge=0, le=500)

    # 0 выключает лимит ответов в одном треде за час
    max_posts_per_thread_hour: int = Field(default=10, ge=0, le=10000)


class IdentifierSettings(BaseModel):
    """границы имён для новых досок и аккаунтов"""

    model_config = ConfigDict(extra="forbid")

    # существующие имена не перепроверяются после изменения этих значений
    board_slug_min_length: int = Field(default=1, ge=1, le=63)
    board_slug_max_length: int = Field(default=16, ge=1, le=63)
    profile_handle_min_length: int = Field(default=3, ge=1, le=63)
    profile_handle_max_length: int = Field(default=32, ge=1, le=63)

    @model_validator(mode="after")
    def validate_ranges(self) -> IdentifierSettings:
        """не дать owner сохранить перевёрнутый диапазон"""

        if self.board_slug_min_length > self.board_slug_max_length:
            raise ValueError("board slug minimum is greater than maximum")
        if self.profile_handle_min_length > self.profile_handle_max_length:
            raise ValueError("profile handle minimum is greater than maximum")
        return self


# общие правила медиа: доска может переопределить только нужные поля
class MediaSettings(BaseModel):
    """общие лимиты вложений для всех будущих клиентов"""

    model_config = ConfigDict(extra="forbid")

    # один пост одинаково ограничен для сайта, приложения и Telegram
    max_attachments_per_post: int = Field(default=10, ge=1, le=20)
    # единый предел не создаёт расхождение между photo, video и sticker
    max_attachment_size_mib: int = Field(default=20, ge=1, le=100)
    # потолок всего поста защищает канал даже когда каждое вложение допустимо
    max_total_size_mib: int = Field(default=200, ge=1, le=500)
    # false закрывает загрузку на всём сайте до проверки конкретной доски
    uploads_enabled: bool = True
    # false оставляет загрузку только staff - пригодится для закрытой доски
    allow_anonymous_uploads: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_size_keys(cls, value: object) -> object:
        """прочитать старую настройку без возврата к разным лимитам типов"""

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
                # старый меньший предел сохраняет более строгую политику до миграции
                values["max_attachment_size_mib"] = min(numbers)
        values.pop("max_photo_size_mib", None)
        values.pop("max_other_media_size_mib", None)
        return values

    @property
    def max_photo_size_mib(self) -> int:
        """дать старому коду единый лимит без разделения по mime type"""

        return self.max_attachment_size_mib

    @property
    def max_other_media_size_mib(self) -> int:
        """дать старому коду единый лимит без разделения по mime type"""

        return self.max_attachment_size_mib


# настройки витрины статистики
class StatisticsSettings(BaseModel):
    """настройки счётчиков"""

    model_config = ConfigDict(extra="forbid")

    # это не срок хранения постов: настройка меняет только число active_post_count
    # короткое окно показывает живую активность, не смешивая её с общим числом постов
    activity_window_hours: int = Field(default=24, ge=1, le=168)


class PostingClaimSettings(BaseModel):
    """срок подготовки медиа до публикации"""

    model_config = ConfigDict(extra="forbid")
    ttl_minutes: int = Field(default=30, ge=1, le=120)


# язык и имя анонима
class LocalizationSettings(BaseModel):
    """текстовые настройки сайта"""

    model_config = ConfigDict(extra="forbid")

    # язык интерфейса по умолчанию
    default_locale: str = Field(
        default="ru",
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
    )

    # имя для постов без указанного автора
    anonymous_name: str = Field(
        default="Аноним",
        min_length=1,
        max_length=500,
    )


# настройки внешнего вида
class AppearanceSettings(BaseModel):
    """настройки темы"""

    model_config = ConfigDict(extra="forbid")

    # имя активной темы
    active_theme: str = Field(
        default="dark",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_-]+$",
    )

    # путь к логотипу относительно media/, например originals/abc.png
    logo_path: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$",
    )

    # путь к файлу favicon
    favicon_path: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$",
    )

    # путь к фоновому изображению сайта
    background_path: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$",
    )

    # CSS-переменные стилей (пары «имя: значение»)
    style_vars: dict[str, str] = Field(default_factory=dict)


# настройки названия сайта, описания и правил
class SiteInfoSettings(BaseModel):
    """название сайта, описание и правила"""

    model_config = ConfigDict(extra="forbid")

    # название сайта, показывается в интерфейсе
    title: str = Field(default="Imageboard Engine", min_length=1, max_length=200)

    # краткое описание, показывается в каталоге и поиске
    description: str = Field(default="", max_length=1000)

    # правила сайта, показываются в интерфейсе
    rules: str = Field(default="", max_length=5000)


# все настройки одного сайта
class SiteSettings(BaseModel):
    """полный набор настроек имиджборды"""

    model_config = ConfigDict(extra="forbid")

    # отдельные группы проще показывать и менять в админке
    thread: ThreadSettings = Field(default_factory=ThreadSettings)
    post: PostSettings = Field(default_factory=PostSettings)
    identifiers: IdentifierSettings = Field(default_factory=IdentifierSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    statistics: StatisticsSettings = Field(default_factory=StatisticsSettings)
    posting_claims: PostingClaimSettings = Field(default_factory=PostingClaimSettings)
    localization: LocalizationSettings = Field(default_factory=LocalizationSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    site: SiteInfoSettings = Field(default_factory=SiteInfoSettings)


def get_default_site_settings() -> SiteSettings:
    """вернуть настройки по умолчанию"""

    return SiteSettings()
