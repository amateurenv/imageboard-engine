"""формы ответов служебных маршрутов"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """ответ маршрута /health"""

    # приложение запущено
    status: str

    # текущий режим работы - development, test или production
    environment: str


class ReadyResponse(BaseModel):
    """ответ маршрута /ready"""

    # postgresql и redis ответили на проверку
    status: str