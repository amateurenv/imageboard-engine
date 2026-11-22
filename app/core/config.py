"""Настройки приложения с проверкой типов.

Этот модуль — единое место для переменных окружения. Благодаря этому код не читает
``os.environ`` в разных местах и не получает настройки с неверным типом незаметно.
"""

# Импорты из стандартной библиотеки Python:
# lru_cache запоминает результат функции, а Literal ограничивает значение списком строк.
from functools import lru_cache
from typing import Literal

# BaseSettings умеет читать поля класса из APP_NAME, APP_ENV и других переменных.
# SettingsConfigDict задаёт правила такого чтения.
from pydantic_settings import BaseSettings, SettingsConfigDict


# Наследование от BaseSettings превращает обычный класс в проверяемую конфигурацию.
class Settings(BaseSettings):
    """Настройки, общие для всего запущенного экземпляра приложения."""

    # Аннотация ``str`` требует строку; значение справа используется по умолчанию.
    # Из окружения его можно заменить переменной APP_NAME.
    app_name: str = "Imageboard Engine"
    # Literal разрешает только эти три режима. Опечатка в APP_ENV даст понятную ошибку.
    app_env: Literal["development", "test", "production"] = "development"
    # Pydantic преобразует строки true/false из .env в настоящий логический тип bool.
    app_debug: bool = False

    # model_config — служебная настройка Pydantic, а не пользовательская переменная.
    model_config = SettingsConfigDict(
        # Сначала Pydantic прочитает локальный файл .env, если он существует.
        env_file=".env",
        # Файл .env сохранён в UTF-8, поэтому русский текст в нём будет читаться верно.
        env_file_encoding="utf-8",
        # Лишние строки в .env игнорируются: это удобно при постепенном развитии проекта.
        extra="ignore",
    )


# Декоратор начинается с @ и меняет поведение функции ниже.
# @lru_cache означает: после первого вызова вернуть тот же объект Settings.
@lru_cache
def get_settings() -> Settings:
    """Создать и вернуть единственный объект настроек для этого процесса."""

    # Вызов класса похож на вызов функции: Settings() создаёт экземпляр и валидирует .env.
    return Settings()
