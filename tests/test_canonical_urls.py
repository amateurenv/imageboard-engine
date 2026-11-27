"""проверка публичных ссылок без технических id"""

from app.core.urls import thread_canonical_url


def test_public_thread_url_uses_board_local_number() -> None:
    """обычная доска не показывает внутренний id треда"""

    assert thread_canonical_url("public", "b", 1) == "/b/1"


def test_profile_thread_url_uses_handle() -> None:
    """display name не участвует в ссылке profile-доски"""

    assert thread_canonical_url("profile", "b", 1) == "/@b/1"


def test_public_and_profile_urls_can_coexist() -> None:
    """одинаковый slug не создаёт конфликт из-за префикса @"""

    assert thread_canonical_url("public", "b", 1) != thread_canonical_url("profile", "b", 1)
