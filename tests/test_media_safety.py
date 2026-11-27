"""проверка безопасных границ будущей загрузки медиа"""

from app.services.media import detect_media_type


def test_media_type_comes_from_file_signature() -> None:
    """jpeg определяется по байтам, а не по заголовку клиента"""

    assert detect_media_type(b"\xff\xd8\xff\xe0" + b"x" * 60) == ("image/jpeg", ".jpg")


def test_unknown_signature_is_rejected() -> None:
    """обычный текст нельзя выдать за картинку"""

    assert detect_media_type(b"not an image") is None


def test_non_image_signatures_are_not_upload_types() -> None:
    """видео и audio не получают обходного пути через старый helper"""

    assert detect_media_type(b"\x00\x00\x00\x18ftypisom") is None
    assert detect_media_type(b"OggS" + b"x" * 60) is None
