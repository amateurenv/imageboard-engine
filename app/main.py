"""Точка входа ASGI-приложения.

ASGI — стандарт связи между асинхронным Python-приложением и веб-сервером Uvicorn.
Команда ``uvicorn app.main:app`` находит переменную ``app`` именно в этом модуле.
"""

# Импорты сторонних библиотек: FastAPI создаёт приложение и маршруты,
# а BaseModel описывает и проверяет структуру данных API.
from fastapi import FastAPI
from pydantic import BaseModel

# Импортируем функцию, чтобы получить настройки из одного центра.
from app.core.config import get_settings

# Скобки вызывают функцию. Результат — уже проверенный объект Settings.
settings = get_settings()

# Создаём единственный объект FastAPI, с которым будет работать весь сайт.
app = FastAPI(
    # title попадёт в заголовок интерактивной документации /docs.
    title=settings.app_name,
    # debug включает подробные страницы ошибок только при APP_DEBUG=true.
    debug=settings.app_debug,
    # version — версия нашего приложения, а не версия FastAPI.
    version="0.1.0",
)


# Класс описывает форму JSON-ответа для проверки состояния сервиса.
class HealthResponse(BaseModel):
    """Структура ответа, который возвращает маршрут проверки состояния."""

    # Поле status обязательно и должно быть строкой.
    status: str
    # Поле environment тоже обязательно и укажет текущий режим приложения.
    environment: str


# @app.get — декоратор, который связывает функцию ниже с GET-запросом на /health.
# response_model проверяет и документирует JSON-ответ, tags группирует маршрут в /docs.
@app.get("/health", response_model=HealthResponse, tags=["system"])
# async def создаёт асинхронную функцию: она не блокирует event loop ожиданием I/O.
# ``-> HealthResponse`` сообщает тип возвращаемого значения людям, IDE и проверкам.
async def healthcheck() -> HealthResponse:
    """Вернуть лёгкий ответ о том, что процесс FastAPI жив.

    Здесь намеренно нет обращения к PostgreSQL или Redis: это именно liveness-проверка.
    Позже добавим отдельную readiness-проверку зависимости от внешних сервисов.
    """

    # Создаём валидируемую модель. FastAPI превратит её в JSON вида {"status": "ok", ...}.
    return HealthResponse(status="ok", environment=settings.app_env)
