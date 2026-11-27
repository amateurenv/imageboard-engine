"""проверка контейнера, размеров и прозрачности статических стикеров"""

import struct
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services.media_policy import MediaPolicyCode, MediaPolicyError
from app.services.sticker_processing import MAX_STICKER_BYTES, decode_sticker, transcode_sticker


def webp(size: tuple[int, int], *, animated: bool = False, mode: str = "RGBA") -> bytes:
    with BytesIO() as buffer:
        first = Image.new(mode, size, (20, 60, 90, 71) if mode == "RGBA" else "red")
        if mode == "RGBA" and size[0] >= 4:
            first.putpixel((1, 0), (20, 60, 90, 0))
            first.putpixel((2, 0), (20, 60, 90, 128))
            first.putpixel((3, 0), (20, 60, 90, 255))
        if animated:
            first.save(
                buffer, "WEBP", save_all=True, append_images=[Image.new(mode, size, "blue")],
                duration=100,
            )
        else:
            first.save(buffer, "WEBP", xmp=b"remove this metadata")
        return buffer.getvalue()


@pytest.mark.parametrize("size", [(512, 1), (1, 512), (512, 512)])
def test_sticker_accepts_both_orientations_and_square(size: tuple[int, int]) -> None:
    with decode_sticker(webp(size)) as decoded:
        assert decoded.size == size
        assert decoded.getpixel((0, 0))[3] == 71


@pytest.mark.parametrize("size", [(511, 511), (513, 512), (512, 513)])
def test_sticker_requires_long_side_of_exactly_512(size: tuple[int, int]) -> None:
    with pytest.raises(MediaPolicyError) as rejected:
        decode_sticker(webp(size))
    assert rejected.value.code == MediaPolicyCode.INVALID_MEDIA


def test_sticker_rejects_animation_invalid_container_and_other_formats() -> None:
    animated = webp((512, 128), animated=True)
    # оставляем в контейнере только первый ANMF - один кадр всё равно не статический WebP
    last_frame = animated.rindex(b"ANMF")
    single_frame = b"RIFF" + struct.pack("<I", last_frame - 8) + animated[8:last_frame]
    for payload in (animated, single_frame, webp((512, 128))[:-1], b"RIFF\x04\0\0\0WEBP"):
        with pytest.raises(MediaPolicyError) as rejected:
            decode_sticker(payload)
        assert rejected.value.code == MediaPolicyCode.INVALID_MEDIA
    with BytesIO() as image:
        Image.new("RGBA", (512, 30)).save(image, "PNG")
        with pytest.raises(MediaPolicyError) as rejected:
            decode_sticker(image.getvalue())
        assert rejected.value.code == MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE


def test_sticker_byte_limit_includes_exact_boundary() -> None:
    original = webp((512, 128))
    padding = MAX_STICKER_BYTES - len(original) - 8
    exact = (
        b"RIFF" + struct.pack("<I", MAX_STICKER_BYTES - 8) + original[8:]
        + b"JUNK" + struct.pack("<I", padding) + bytes(padding)
    )
    with decode_sticker(exact) as decoded:
        assert decoded.size == (512, 128)
    with pytest.raises(MediaPolicyError) as rejected:
        decode_sticker(exact + b"\0")
    assert rejected.value.code == MediaPolicyCode.MEDIA_FILE_TOO_LARGE


def test_sticker_rejects_partial_chunk_header() -> None:
    original = webp((512, 128))
    # внешний размер riff совпадает, но последний блок не содержит полного заголовка
    truncated = b"RIFF" + struct.pack("<I", len(original) - 4) + original[8:] + b"JUNK"
    with pytest.raises(MediaPolicyError, match="обрезан заголовок блока WebP") as rejected:
        decode_sticker(truncated)
    assert rejected.value.code == MediaPolicyCode.INVALID_MEDIA


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_canonical_sticker_strips_metadata_and_preserves_alpha(tmp_path: Path, mode: str) -> None:
    source, target = tmp_path / "source", tmp_path / "display"
    source.write_bytes(webp((128, 512), mode=mode))
    transcode_sticker(source, target)
    assert target.stat().st_size <= MAX_STICKER_BYTES
    with Image.open(target) as canonical, decode_sticker(source.read_bytes()) as original:
        assert canonical.format == "WEBP"
        assert canonical.size == original.size
        assert b"remove this metadata" not in target.read_bytes()
        alpha = canonical.convert("RGBA").getchannel("A")
        assert alpha.tobytes() == original.getchannel("A").tobytes()
