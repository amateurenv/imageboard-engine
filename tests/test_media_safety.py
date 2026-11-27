"""проверка безопасных границ будущей загрузки медиа"""

from io import BytesIO
from typing import cast

import pytest
from fastapi import HTTPException, UploadFile
from redis.asyncio import Redis

from app.repositories.media import MediaRepository
from app.services.media import MediaService, detect_media_type


def test_media_type_comes_from_file_signature() -> None:
    """jpeg определяется по байтам, а не по заголовку клиента"""

    assert detect_media_type(b"\xff\xd8\xff\xe0" + b"x" * 60) == ("image/jpeg", ".jpg")


def test_unknown_signature_is_rejected() -> None:
    """обычный текст нельзя выдать за картинку"""

    assert detect_media_type(b"not an image") is None


def test_mp4_and_ogg_signatures_are_distinguished() -> None:
    """контейнеры получают своё расширение только после проверки байтов"""

    assert detect_media_type(b"\x00\x00\x00\x18ftypisom") == ("video/mp4", ".mp4")
    assert detect_media_type(b"OggS" + b"x" * 60) == ("audio/ogg", ".ogg")


@pytest.mark.anyio
async def test_rejected_upload_is_closed() -> None:
    """отклонённый upload не оставляет открытый временный файл"""

    upload = UploadFile(filename="not-media.txt", file=BytesIO(b"not an image"))
    service = MediaService(
        cast(MediaRepository, object()),
        cast(Redis, object()),
    )

    with pytest.raises(HTTPException, match="file signature"):
        await service.upload(upload)

    assert upload.file.closed
