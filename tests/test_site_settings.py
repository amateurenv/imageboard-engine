"""проверка настроек сайта"""

import pytest
from pydantic import ValidationError

from app.core.site_settings import SiteSettings, get_default_site_settings


def test_default_site_settings() -> None:
    """настройки по умолчанию содержат ожидаемые значения"""

    settings = get_default_site_settings()

    assert settings.thread.max_subject_length == 200
    assert settings.post.max_post_length == 10000
    assert settings.post.max_author_name_length == 50
    assert settings.localization.default_locale == "ru"
    assert settings.localization.anonymous_name == "Аноним"
    assert settings.appearance.active_theme == "dark"


def test_site_settings_reject_too_long_post() -> None:
    """слишком большой лимит поста не проходит проверку"""

    with pytest.raises(ValidationError):
        SiteSettings.model_validate(
            {
                "post": {
                    "max_post_length": 100001,
                }
            }
        )


def test_site_settings_reject_unknown_setting() -> None:
    """лишняя настройка не проходит проверку"""

    with pytest.raises(ValidationError):
        SiteSettings.model_validate(
            {
                "post": {
                    "unknown_limit": 123,
                }
            }
        )