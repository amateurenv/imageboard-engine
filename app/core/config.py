"""настройки приложения с проверкой типов"""

# lru_cache сохраняет результат, Literal ограничивает список допустимых строк
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

# BaseSettings читает переменные окружения, SettingsConfigDict задаёт правила
from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# BaseSettings превращает этот класс в проверяемые настройки
class Settings(BaseSettings):
    """настройки приложения"""

    # имя приложения, APP_NAME в .env может его заменить
    app_name: str = "Imageboard Engine"
    # режим работы, другие значения не пройдут проверку
    app_env: Literal["development", "test", "production"] = "development"
    # debug режим, строка true или false из .env станет bool
    app_debug: bool = False

    # IP доверенных reverse proxy через запятую, пустая строка безопасна по умолчанию
    trusted_proxy_ips: str = ""

    # адрес postgresql из DATABASE_URL в .env
    database_url: PostgresDsn

    # адрес redis из REDIS_URL в .env
    redis_url: RedisDsn

    # корень закрытого media storage, не подключать к static или reverse proxy на этом этапе
    media_storage_root: Path = Path("media")
    media_storage_max_bytes: int = Field(default=8 * 1024**3, gt=0, le=2**63 - 1)
    media_storage_min_free_bytes: int = Field(default=256 * 1024**2, gt=0, le=2**63 - 1)
    media_upload_max_file_bytes: int = Field(
        default=20 * 1024**2, ge=1024**2, le=100 * 1024**2
    )
    media_upload_concurrency: int = Field(default=4, ge=1, le=64)
    media_upload_timeout_seconds: int = Field(default=120, ge=1, le=900)

    # имена бинарников задаются оператором, но никогда не становятся частью shell-команды
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    @model_validator(mode="after")
    def reject_debug_in_production(self) -> Self:
        """запретить подробные ошибки в production"""

        # traceback не должен попасть в ответ реальному посетителю
        if self.app_env == "production" and self.app_debug:
            raise ValueError("APP_DEBUG must be false when APP_ENV=production")
        if self.media_storage_max_bytes <= self.media_ingress_budget:
            raise ValueError("MEDIA_STORAGE_MAX_BYTES must exceed the multipart budget")
        if self.app_env == "production" and not self.media_storage_root.is_absolute():
            raise ValueError("MEDIA_STORAGE_ROOT must be an absolute mount path in production")
        return self

    @property
    def media_upload_body_bytes(self) -> int:
        """верхняя граница тела с запасом на multipart"""

        return self.media_upload_max_file_bytes + 64 * 1024

    @property
    def media_ingress_budget(self) -> int:
        """доля квоты для всех одновременно открытых multipart файлов"""

        blocks = 64 * 1024 * (self.media_upload_body_bytes // (1024**2) + 1)
        return self.media_upload_concurrency * (self.media_upload_body_bytes + blocks)

    @property
    def media_ingress_file_slots(self) -> int:
        """максимум файлов multipart, которые могут перейти из памяти на диск"""

        return self.media_upload_concurrency * (self.media_upload_body_bytes // (1024**2) + 1)

    # правила чтения настроек
    model_config = SettingsConfigDict(
        # читать локальный .env, если он есть
        env_file=".env",
        # .env сохранён в utf-8
        env_file_encoding="utf-8",
        # не падать из-за лишних строк в .env
        extra="ignore",
    )


# после первого вызова возвращать те же настройки
@lru_cache
def get_settings() -> Settings:
    """создать настройки один раз для всего процесса"""

    # Settings читает .env и проверяет типы
    # noinspection PyArgumentList
    # mypy не видит значения из .env, а pydantic проверит их при создании Settings
    return Settings()  # type: ignore[call-arg]
