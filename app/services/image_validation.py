"""проверка изображений до и во время безопасной обработки"""

import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError, MediaType

MAX_IMAGE_WIDTH = 12_000
MAX_IMAGE_HEIGHT = 12_000
MAX_IMAGE_PIXELS = 20_000_000


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """сведения о реально декодированном изображении"""

    mime_type: str
    suffix: str
    width: int
    height: int
    media_type: MediaType = MediaType.PHOTO


_FORMAT_DETAILS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


def _validate_container(path: Path, image_format: str) -> None:
    """отклонить простой appended polyglot до создания производных файлов"""

    size = path.stat().st_size
    with path.open("rb") as source:
        if image_format == "JPEG":
            source.seek(-2, 2)
            if source.read(2) != b"\xff\xd9":
                raise ValueError("jpeg end marker is missing")
        if image_format == "PNG":
            source.seek(-8, 2)
            if source.read(8) != b"IEND\xaeB`\x82":
                raise ValueError("png has trailing or incomplete data")
        if image_format == "WEBP":
            header = source.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
                raise ValueError("webp header is invalid")
            declared_size = int.from_bytes(header[4:8], "little") + 8
            if declared_size != size:
                raise ValueError("webp has trailing or incomplete data")


def validate_image(path: Path) -> ValidatedImage:
    """декодировать только jpeg png webp и проверить безопасные пределы"""

    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            with Image.open(path) as image:
                image_format = image.format or ""
                details = _FORMAT_DETAILS.get(image_format)
                if details is None:
                    raise MediaPolicyError(
                        code=MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE,
                        message="на этом этапе поддерживаются только JPEG, PNG и WebP",
                        http_status=415,
                    )
                width, height = image.size
                if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
                    raise ValueError("image dimensions exceed the allowed limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image pixel count exceeds the allowed limit")
                image.verify()
        _validate_container(path, image_format)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as decoded:
                decoded.load()
        return ValidatedImage(details[0], details[1], width, height)
    except MediaPolicyError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as error:
        raise MediaPolicy.invalid_media() from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels
