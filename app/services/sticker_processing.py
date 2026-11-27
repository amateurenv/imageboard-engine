"""проверка и серверное перекодирование статического WebP для стикеров"""

from io import BytesIO
from pathlib import Path

from PIL import Image

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError

STICKER_MIME = "image/webp"
MAX_STICKER_BYTES = 512 * 1024
STICKER_SIDE = 512
STICKER_REPLY_WARNING = "Стикер можно отправить только в качестве ответа в существующем треде"


def _check_dimensions(width: int, height: int) -> None:
    """ограничить размеры холста и кадра до передачи файла декодеру"""

    if min(width, height) < 1 or max(width, height) != STICKER_SIDE:
        raise MediaPolicy.invalid_media("одна сторона стикера должна быть ровно 512 px")


def decode_sticker(data: bytes) -> Image.Image:
    """декодировать ровно один статический кадр с сохранением альфа-канала"""

    if len(data) > MAX_STICKER_BYTES:
        raise MediaPolicyError(
            code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
            message="размер стикера больше 512 КиБ",
            http_status=413,
            limit_bytes=MAX_STICKER_BYTES,
        )
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MediaPolicyError(
            code=MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE,
            message="стикер должен быть статическим WebP",
            http_status=415,
        )
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise MediaPolicy.invalid_media("некорректный размер контейнера WebP")

    # проверяем контейнер до Pillow - WebP с одним анимированным кадром тоже запрещён
    offset = 12
    while offset < len(data):
        if len(data) - offset < 8:
            raise MediaPolicy.invalid_media("обрезан заголовок блока WebP")
        tag = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        start = offset + 8
        offset = start + size + size % 2
        if offset > len(data):
            raise MediaPolicy.invalid_media("повреждён блок WebP")
        if tag in {b"ANIM", b"ANMF"} or (tag == b"VP8X" and size and data[start] & 2):
            raise MediaPolicy.invalid_media("анимированные стикеры не поддерживаются")
        if tag == b"VP8X" and size == 10:
            _check_dimensions(
                int.from_bytes(data[start + 4:start + 7], "little") + 1,
                int.from_bytes(data[start + 7:start + 10], "little") + 1,
            )
        if tag == b"VP8 " and size >= 10:
            _check_dimensions(
                int.from_bytes(data[start + 6:start + 8], "little") & 0x3FFF,
                int.from_bytes(data[start + 8:start + 10], "little") & 0x3FFF,
            )
        if tag == b"VP8L" and size >= 5:
            dimensions = int.from_bytes(data[start + 1:start + 5], "little")
            _check_dimensions((dimensions & 0x3FFF) + 1, ((dimensions >> 14) & 0x3FFF) + 1)

    try:
        with Image.open(BytesIO(data), formats=["WEBP"]) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise MediaPolicy.invalid_media("анимированные стикеры не поддерживаются")
            _check_dimensions(*source.size)
            # convert декодирует кадр целиком и оставляет цвет и прозрачность в пикселях
            return source.convert("RGBA")
    except (EOFError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise MediaPolicy.invalid_media("не удалось декодировать стикер") from error


def transcode_sticker(source: Path, target: Path) -> None:
    """записать новый WebP без исходных метаданных с прежними размерами и альфа-каналом"""

    with source.open("rb") as original:
        image = decode_sticker(original.read(MAX_STICKER_BYTES + 1))
    with image:
        # цвет можно сжать сильнее, альфа-канал WebP кодируется без потерь
        for quality in (85, 65, 40):
            encoded = BytesIO()
            image.save(encoded, "WEBP", quality=quality, method=6)
            if encoded.tell() <= MAX_STICKER_BYTES:
                target.write_bytes(encoded.getvalue())
                return
    raise MediaPolicy.invalid_media("готовый стикер превышает 512 КиБ")
