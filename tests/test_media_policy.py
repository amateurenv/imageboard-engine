"""проверка единой policy вложений без файлов, сети и Telegram API"""

import pytest

from app.core.site_settings import MediaSettings
from app.schemas.boards import BoardMediaOverrides
from app.services.media_policy import (
    MEBIBYTE,
    CustomEmojiEntity,
    MediaAttachment,
    MediaPolicy,
    MediaPolicyCode,
    MediaPolicyError,
    MediaType,
)


def default_policy() -> MediaPolicy:
    """получить обычную policy без переопределений доски"""

    return MediaPolicy.from_settings(MediaSettings())


def assert_error(error: pytest.ExceptionInfo[MediaPolicyError], code: MediaPolicyCode) -> None:
    """проверить стабильный код, а не только русское сообщение"""

    assert error.value.code is code


def test_attachment_limit_is_twenty_mib_and_exposes_bytes() -> None:
    """клиент получает точный байтовый лимит для отображения своей ошибки"""

    with pytest.raises(MediaPolicyError) as error:
        default_policy().validate(
            [MediaAttachment(MediaType.VIDEO, 20 * MEBIBYTE + 1)]
        )

    assert_error(error, MediaPolicyCode.MEDIA_FILE_TOO_LARGE)
    assert error.value.http_status == 413
    assert error.value.to_detail()["limit_bytes"] == 20 * MEBIBYTE


def test_total_post_limit_reports_total_size_error() -> None:
    """общий лимит проверяется после каждого отдельного лимита"""

    policy = MediaPolicy.from_settings(
        MediaSettings(max_attachments_per_post=11, max_total_size_mib=200)
    )
    attachments = [MediaAttachment(MediaType.PHOTO, 20 * MEBIBYTE) for _ in range(10)]
    attachments.append(MediaAttachment(MediaType.PHOTO, 1))

    with pytest.raises(MediaPolicyError) as error:
        policy.validate(attachments)

    assert_error(error, MediaPolicyCode.MEDIA_POST_TOO_LARGE)
    assert error.value.to_detail()["limit_bytes"] == 200 * MEBIBYTE


def test_more_than_ten_attachments_is_rejected() -> None:
    """лимит альбома одинаков для всех будущих клиентов"""

    attachments = [MediaAttachment(MediaType.STICKER, 1) for _ in range(11)]

    with pytest.raises(MediaPolicyError) as error:
        default_policy().validate(attachments)

    assert_error(error, MediaPolicyCode.MEDIA_COUNT_EXCEEDED)
    assert error.value.http_status == 422


@pytest.mark.parametrize("media_type", ["document", "application/zip"])
def test_unsupported_attachment_type_is_rejected(media_type: str) -> None:
    """документы и произвольные файлы не становятся вложениями"""

    with pytest.raises(MediaPolicyError) as error:
        default_policy().validate([MediaAttachment(media_type, 1)])

    assert_error(error, MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE)
    assert error.value.http_status == 415


def test_board_can_disable_uploads() -> None:
    """доска может закрыть вложения, не меняя общую policy сайта"""

    policy = MediaPolicy.from_settings(
        MediaSettings(), BoardMediaOverrides(uploads_enabled=False)
    )

    with pytest.raises(MediaPolicyError) as error:
        policy.validate([MediaAttachment(MediaType.PHOTO, 1)])

    assert_error(error, MediaPolicyCode.MEDIA_UPLOAD_DISABLED)
    assert error.value.http_status == 403


def test_custom_emoji_is_text_entity_and_not_attachment() -> None:
    """custom emoji не расходует лимит вложений и хранит текстовый fallback"""

    emoji = CustomEmojiEntity(custom_emoji_id="5368324170671202286", fallback_text="❤️")
    attachments = [MediaAttachment(MediaType.STICKER, 1) for _ in range(10)]
    default_policy().validate(attachments, custom_emoji_entities=[emoji])


def test_board_override_changes_effective_limit() -> None:
    """доска может сделать лимит строже глобального без отдельной policy"""

    policy = MediaPolicy.from_settings(
        MediaSettings(), BoardMediaOverrides(max_attachment_size_mib=5)
    )

    with pytest.raises(MediaPolicyError) as error:
        policy.validate([MediaAttachment(MediaType.VIDEO_NOTE, 5 * MEBIBYTE + 1)])

    assert_error(error, MediaPolicyCode.MEDIA_FILE_TOO_LARGE)
    assert error.value.to_detail()["limit_bytes"] == 5 * MEBIBYTE


def test_legacy_override_is_normalized_to_single_limit() -> None:
    """старый ключ доски не возвращает разделение лимитов после обновления"""

    override = BoardMediaOverrides(max_photo_size_mib=7)

    assert override.max_attachment_size_mib == 7
    assert override.model_dump() == {
        "max_attachments_per_post": None,
        "max_attachment_size_mib": 7,
        "max_total_size_mib": None,
        "uploads_enabled": None,
        "allow_anonymous_uploads": None,
    }
