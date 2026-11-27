"""минимальная проверка image-сигнатуры до полной Pillow-валидации"""


def detect_media_type(header: bytes) -> tuple[str, str] | None:
    """распознать только поддерживаемые image заголовки без доверия client mime"""

    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None
