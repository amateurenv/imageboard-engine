"""проверка настроек сайта"""

import pytest
from pydantic import ValidationError

from app.core.site_settings import SiteSettings, get_default_site_settings


def test_default_site_settings() -> None:
    """настройки по умолчанию содержат ожидаемые значения"""

    settings = get_default_site_settings()

    assert settings.thread.max_subject_length == 200
    assert settings.thread.max_threads_per_board_day == 3
    assert settings.post.max_post_length == 10000
    assert settings.post.max_author_name_length == 50
    assert settings.post.max_posts_per_thread_hour == 10
    assert settings.media.max_attachments_per_post == 10
    assert settings.media.max_attachment_size_mib == 20
    assert settings.media.max_photo_size_mib == 20
    assert settings.media.max_other_media_size_mib == 20
    assert settings.media.max_total_size_mib == 200
    assert settings.media.allow_anonymous_uploads is True
    assert settings.statistics.activity_window_hours == 24
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


def test_site_settings_rejects_unsafe_media_ceiling() -> None:
    """глобальный лимит не даёт случайно разрешить бесконечный upload"""

    with pytest.raises(ValidationError):
        SiteSettings.model_validate({"media": {"max_attachments_per_post": 21}})
