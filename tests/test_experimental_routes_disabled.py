"""проверка временно выключенных экспериментальных маршрутов"""

from app.main import app


def test_media_and_realtime_routes_are_not_registered() -> None:
    """небезопасные медиа и realtime маршруты не доступны публично"""

    registered_paths = {
        route.path
        for route in app.routes
        if hasattr(route, "path")
    }

    assert "/api/v1/media" not in registered_paths
    assert "/api/v1/ws" not in registered_paths
