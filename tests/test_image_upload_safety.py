"""unit-проверки image-only quarantine без postgres и redis"""

from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from fastapi import UploadFile
from PIL import Image
from redis.asyncio import Redis

from app.core.site_settings import MediaSettings, SiteSettings
from app.services import image_validation
from app.services.image_validation import validate_image
from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.media_upload import MediaUploadService
from scripts import media_worker
from scripts.media_worker import make_derivatives


def make_image(image_format: str, size: tuple[int, int] = (8, 8)) -> bytes:
    """создать корректный image без файлов пользователя"""

    output = BytesIO()
    Image.new("RGB", size, "red").save(output, image_format)
    return output.getvalue()


def make_animation(
    image_format: str, *, frames: int = 2, duration: int = 100, size: tuple[int, int] = (8, 8)
) -> bytes:
    """создать короткую анимацию только для decoder и worker тестов"""

    images = [
        Image.new("RGB", size, (255 if index % 2 else 0, 0, 255 if index % 2 == 0 else 0))
        for index in range(frames)
    ]
    output = BytesIO()
    images[0].save(
        output,
        image_format,
        save_all=True,
        append_images=images[1:],
        duration=duration,
        loop=0,
        optimize=False,
    )
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
        ("GIF", "image/gif"),
    ],
)
def test_only_expected_images_pass_content_validation(
    tmp_path: Path, image_format: str, mime_type: str
) -> None:
    """проверка использует decoder и не зависит от расширения имени"""

    path = tmp_path / "unknown-name"
    path.write_bytes(make_image(image_format))

    assert validate_image(path).mime_type == mime_type


@pytest.mark.parametrize("image_format", ["GIF", "WEBP"])
def test_animated_gif_and_webp_are_detected_from_content(
    tmp_path: Path, image_format: str
) -> None:
    """анимация определяется decoder, а не расширением или multipart MIME"""

    path = tmp_path / "pretend-static.jpg"
    path.write_bytes(make_animation(image_format))

    validated = validate_image(path)

    assert validated.is_animation is True
    assert validated.media_type.value == "animation"


def test_static_webp_is_not_marked_as_animation(tmp_path: Path) -> None:
    """однокадровый WebP остаётся обычным photo для клиента и policy"""

    path = tmp_path / "still.webp"
    path.write_bytes(make_image("WEBP"))

    assert validate_image(path).is_animation is False


def test_broken_or_polyglot_image_is_rejected(tmp_path: Path) -> None:
    """повреждённый или appended container не доходит до worker"""

    gif = tmp_path / "image.gif"
    gif.write_bytes(make_animation("GIF")[:-1])
    with pytest.raises(MediaPolicyError) as invalid_gif:
        validate_image(gif)
    assert invalid_gif.value.code is MediaPolicyCode.INVALID_MEDIA

    polyglot = tmp_path / "image.png"
    polyglot.write_bytes(make_image("PNG") + b"not-an-image-payload")
    with pytest.raises(MediaPolicyError) as invalid:
        validate_image(polyglot)
    assert invalid.value.code is MediaPolicyCode.INVALID_MEDIA


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00\x00\x00\x18ftypisom",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"fLaC\x00\x00\x00\x22",
        b"\x1aE\xdf\xa3\x93B\x82\x88webm",
        b"PK\x03\x04archive",
    ],
)
def test_known_disallowed_containers_return_unsupported_type(
    tmp_path: Path, payload: bytes
) -> None:
    """video wav flac webm и archive не доходят до дорогого decoder"""

    path = tmp_path / "fake-name.jpg"
    path.write_bytes(payload)

    with pytest.raises(MediaPolicyError) as error:
        validate_image(path)

    assert error.value.code is MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE


def test_unknown_bytes_return_invalid_media(tmp_path: Path) -> None:
    """неизвестные байты не становятся поддерживаемым type из-за расширения"""

    path = tmp_path / "audio.mp3"
    path.write_bytes(b"random unknown bytes")

    with pytest.raises(MediaPolicyError) as error:
        validate_image(path)

    assert error.value.code is MediaPolicyCode.INVALID_MEDIA


def test_dimension_limit_rejects_small_file_with_excessive_width(tmp_path: Path) -> None:
    """разрешение проверяется отдельно от размера upload"""

    path = tmp_path / "wide.png"
    path.write_bytes(make_image("PNG", (12_001, 1)))
    with pytest.raises(MediaPolicyError) as error:
        validate_image(path)
    assert error.value.code is MediaPolicyCode.INVALID_MEDIA


@pytest.mark.parametrize(
    ("attribute", "value", "animation"),
    [
        ("MAX_ANIMATION_FRAME_SIDE", 7, {"size": (8, 8)}),
        ("MAX_ANIMATION_FRAME_PIXELS", 63, {"size": (8, 8)}),
        ("MAX_ANIMATION_FRAMES", 1, {"frames": 2}),
        ("MAX_ANIMATION_DURATION_MS", 100, {"duration": 101}),
        ("MAX_ANIMATION_DECODED_PIXELS", 100, {"frames": 2, "size": (8, 8)}),
    ],
)
def test_animation_resource_limits_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: int,
    animation: dict[str, object],
) -> None:
    """каждый animation budget возвращает единый доменный invalid_media"""

    path = tmp_path / "large.gif"
    path.write_bytes(make_animation("GIF", **animation))
    monkeypatch.setattr(image_validation, attribute, value)

    with pytest.raises(MediaPolicyError) as error:
        validate_image(path)

    assert error.value.code is MediaPolicyCode.INVALID_MEDIA


@pytest.mark.anyio
async def test_stream_limit_removes_partial_quarantine_file(tmp_path: Path) -> None:
    """upload без content-length всё равно прекращается по фактическим байтам"""

    storage = MediaStorage(tmp_path)
    service = MediaUploadService(
        cast(object, object()),
        cast(Redis, object()),
        SiteSettings(),
        tmp_path,
    )
    upload = UploadFile(filename="big.jpg", file=BytesIO(b"x" * 3))
    policy = MediaPolicy.from_settings(MediaSettings(max_attachment_size_mib=1))
    policy = MediaPolicy(
        max_attachments_per_post=policy.max_attachments_per_post,
        max_attachment_size_bytes=2,
        max_total_size_bytes=policy.max_total_size_bytes,
        uploads_enabled=True,
        allow_anonymous_uploads=True,
    )

    with pytest.raises(MediaPolicyError) as error:
        await service._stream_to_quarantine(upload, "quarantine/limited.image", policy)
    assert error.value.code is MediaPolicyCode.MEDIA_FILE_TOO_LARGE
    assert not storage.resolve("quarantine/limited.image").exists()


def test_worker_derivatives_stay_inside_storage_and_drop_metadata(tmp_path: Path) -> None:
    """worker создаёт только controlled WebP paths, а не выдаёт оригинал"""

    storage = MediaStorage(tmp_path)
    source = storage.resolve("quarantine/source.jpg")
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(make_image("JPEG"))

    display_path, preview_path = make_derivatives(str(source), storage, 42)

    assert display_path == "display/42.webp"
    assert preview_path == "previews/42.webp"
    with Image.open(storage.resolve(display_path)) as display:
        assert display.format == "WEBP"
        assert not display.getexif()


@pytest.mark.parametrize("image_format", ["GIF", "WEBP"])
def test_worker_reencodes_animation_to_animated_display_and_static_preview(
    tmp_path: Path, image_format: str
) -> None:
    """animation display остаётся animated WebP, а preview берёт только первый кадр"""

    storage = MediaStorage(tmp_path)
    source = storage.resolve(f"quarantine/source.{image_format.lower()}")
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(make_animation(image_format))

    display_path, preview_path = make_derivatives(str(source), storage, 43, True)

    with Image.open(storage.resolve(display_path)) as display:
        assert display.format == "WEBP"
        assert display.n_frames == 2
    with Image.open(storage.resolve(preview_path)) as preview:
        assert preview.format == "WEBP"
        assert preview.n_frames == 1


def test_worker_removes_partial_derivatives_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ошибка preview не оставляет уже созданный display рядом с quarantine"""

    storage = MediaStorage(tmp_path)
    source = storage.resolve("quarantine/source.jpg")
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(make_image("JPEG"))
    original_save = media_worker._save_webp
    calls = 0

    def fail_second_save(image: Image.Image, target: MediaStorage, relative_path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk is temporarily unavailable")
        original_save(image, target, relative_path)

    monkeypatch.setattr(media_worker, "_save_webp", fail_second_save)
    with pytest.raises(OSError, match="temporarily unavailable"):
        make_derivatives(str(source), storage, 99)

    assert not storage.resolve("display/99.webp").exists()
    assert not storage.resolve("previews/99.webp").exists()


def test_storage_never_follows_symlink_or_path_traversal(tmp_path: Path) -> None:
    """cleanup не может удалить файл за пределами configured root"""

    storage = MediaStorage(tmp_path / "media")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    with pytest.raises(MediaStorageError):
        storage.resolve("../outside.txt")

    link = storage.resolve("quarantine/link")
    link.parent.mkdir(mode=0o700, parents=True)
    link.symlink_to(outside)
    with pytest.raises(MediaStorageError):
        storage.delete_file("quarantine/link")
    assert outside.read_text(encoding="utf-8") == "keep"
