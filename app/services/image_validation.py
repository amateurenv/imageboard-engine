"""проверка изображений до и во время безопасной обработки"""

import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError, MediaType

# статичная картинка может быть крупнее, потому что worker уменьшит display версию
MAX_IMAGE_WIDTH = 12_000
MAX_IMAGE_HEIGHT = 12_000
MAX_IMAGE_PIXELS = 20_000_000

# анимация декодируется много раз, поэтому для неё нужен отдельный меньший budget
MAX_ANIMATION_FRAME_SIDE = 4_096
MAX_ANIMATION_FRAME_PIXELS = 8_000_000
MAX_ANIMATION_FRAMES = 300
MAX_ANIMATION_DURATION_MS = 30_000
MAX_ANIMATION_DECODED_PIXELS = 100_000_000


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """сведения о реально декодированном изображении"""

    mime_type: str
    suffix: str
    width: int
    height: int
    media_type: MediaType = MediaType.PHOTO
    is_animation: bool = False


_FORMAT_DETAILS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}


def _looks_like_known_non_image(path: Path) -> bool:
    """отделить известный запрещённый container от повреждённого псевдо-image"""

    with path.open("rb") as source:
        header = source.read(16)
    return (
        (len(header) >= 8 and header[4:8] == b"ftyp")
        or header.startswith((b"OggS", b"ID3", b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf'\x1c"))
        or header.startswith(b"MZ")
    )


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
        if image_format == "GIF":
            source.seek(-1, 2)
            if source.read(1) != b";":
                raise ValueError("gif has trailing or incomplete data")


def _normal_frame_duration(value: object) -> int:
    """принять decoder duration без неявного преобразования странных значений"""

    if not isinstance(value, int) or value < 0:
        raise ValueError("animation frame duration is invalid")
    return value


def _validate_animation(image: Image.Image) -> tuple[int, int]:
    """декодировать кадры по одному и проверить общий ресурсный budget"""

    frame_count = getattr(image, "n_frames", 1)
    if frame_count < 2:
        return 1, 0
    if frame_count > MAX_ANIMATION_FRAMES:
        raise ValueError("animation frame count exceeds the allowed limit")

    total_duration = 0
    decoded_pixels = 0
    for frame_index in range(frame_count):
        image.seek(frame_index)
        width, height = image.size
        if width > MAX_ANIMATION_FRAME_SIDE or height > MAX_ANIMATION_FRAME_SIDE:
            raise ValueError("animation frame dimensions exceed the allowed limit")
        frame_pixels = width * height
        if frame_pixels > MAX_ANIMATION_FRAME_PIXELS:
            raise ValueError("animation frame pixel count exceeds the allowed limit")
        decoded_pixels += frame_pixels
        if decoded_pixels > MAX_ANIMATION_DECODED_PIXELS:
            raise ValueError("animation decoded pixel budget exceeds the allowed limit")
        # load вызывается для каждого кадра - одной проверки заголовка GIF/WebP недостаточно
        image.load()
        total_duration += _normal_frame_duration(image.info.get("duration", 0))
        if total_duration > MAX_ANIMATION_DURATION_MS:
            raise ValueError("animation duration exceeds the allowed limit")
    return frame_count, total_duration


def validate_image(path: Path) -> ValidatedImage:
    """декодировать только разрешённые image и проверить безопасные пределы"""

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
                        message="на этом этапе поддерживаются только JPEG, PNG, WebP и GIF",
                        http_status=415,
                    )
                width, height = image.size
                is_animation = getattr(image, "n_frames", 1) > 1
                if is_animation:
                    _validate_animation(image)
                else:
                    if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
                        raise ValueError("image dimensions exceed the allowed limit")
                    if width * height > MAX_IMAGE_PIXELS:
                        raise ValueError("image pixel count exceeds the allowed limit")
                    image.verify()
        _validate_container(path, image_format)
        # verify не декодирует static image полностью, поэтому открываем файл второй раз
        if not is_animation:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as decoded:
                    decoded.load()
        return ValidatedImage(
            details[0],
            details[1],
            width,
            height,
            MediaType.ANIMATION if is_animation else MediaType.PHOTO,
            is_animation,
        )
    except MediaPolicyError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as error:
        if _looks_like_known_non_image(path):
            raise MediaPolicyError(
                code=MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE,
                message="этот тип вложения пока не поддерживается",
                http_status=415,
            ) from error
        raise MediaPolicy.invalid_media() from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels
