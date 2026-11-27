"""проверка выключенных generic media и realtime маршрутов"""

from app.main import app


def test_generic_media_and_realtime_routes_are_not_registered() -> None:
    """узкий board upload не включает старый generic media или realtime маршрут"""

    registered_paths = {
        route.path
        for route in app.routes
        if hasattr(route, "path")
    }

    assert "/api/v1/media" not in registered_paths
    assert "/api/v1/ws" not in registered_paths
