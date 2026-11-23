"""настройки приложения с проверкой типов"""

# lru_cache сохраняет результат, Literal ограничивает список допустимых строк
from functools import lru_cache
from typing import Literal

# BaseSettings читает переменные окружения, SettingsConfigDict задаёт правила
from pydantic import PostgresDsn, RedisDsn
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

    # адрес postgresql из DATABASE_URL в .env
    database_url: PostgresDsn

    # адрес redis из REDIS_URL в .env
    redis_url: RedisDsn

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
    return Settings()
