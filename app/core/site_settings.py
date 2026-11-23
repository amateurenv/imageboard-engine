"""правила и значения по умолчанию для имиджборды"""

from pydantic import BaseModel, ConfigDict, Field


# настройки создания тредов
class ThreadSettings(BaseModel):
    """лимиты для тредов"""

    # неизвестные поля в настройках считаются ошибкой
    model_config = ConfigDict(extra="forbid")

    # 0 разрешает тред без заголовка
    max_subject_length: int = Field(default=200, ge=0, le=1000)


# настройки создания постов
class PostSettings(BaseModel):
    """лимиты для постов"""

    model_config = ConfigDict(extra="forbid")

    # лимит текста одного поста
    max_post_length: int = Field(default=10000, ge=1, le=100000)

    # 0 запрещает собственное имя автора
    max_author_name_length: int = Field(default=50, ge=0, le=500)


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


# все настройки одного сайта
class SiteSettings(BaseModel):
    """полный набор настроек имиджборды"""

    model_config = ConfigDict(extra="forbid")

    # отдельные группы проще показывать и менять в админке
    thread: ThreadSettings = Field(default_factory=ThreadSettings)
    post: PostSettings = Field(default_factory=PostSettings)
    localization: LocalizationSettings = Field(default_factory=LocalizationSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)


def get_default_site_settings() -> SiteSettings:
    """вернуть настройки по умолчанию"""

    return SiteSettings()