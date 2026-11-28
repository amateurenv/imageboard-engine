"""проверка границ WebM и прозрачности канонических производных"""

import json
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from app.services import video_sticker_processing as webm
from app.services.media_policy import MediaPolicyCode, MediaPolicyError

WEBM_HEADER = bytes.fromhex("1a45dfa3 87 4282 84 7765626d 18538067 81 00")
WEBM_TOOLS_AVAILABLE = all(
    shutil.which(binary) for binary in ("ffmpeg", "ffprobe", "/usr/bin/prlimit")
)


@pytest.mark.parametrize(
    ("stream_changes", "format_changes", "timestamps", "extra_stream"),
    [
        ({"width": 511}, {}, [0], False),
        ({"height": 513}, {}, [0], False),
        ({"codec_name": "vp8"}, {}, [0], False),
        ({"avg_frame_rate": "31/1"}, {}, [0], False),
        ({}, {"duration": "-1"}, [0], False),
        ({}, {"duration": "3.001"}, [0], False),
        ({}, {}, [0, 10], False),
        ({}, {}, [0, 0], False),
        ({}, {}, [-1], False),
        ({}, {}, [i * 33 for i in range(91)], False),
        ({}, {}, [0, 2990], False),
        ({}, {}, [0], True),
    ],
)
def test_rejects_invalid_webm_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_changes: dict,
    format_changes: dict, timestamps: list[int], extra_stream: bool,
) -> None:
    source = tmp_path / "quarantine.upload"
    source.write_bytes(WEBM_HEADER)
    streams = [{
        "codec_type": "video", "codec_name": "vp9", "width": 512, "height": 128,
        "time_base": "1/1000", "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
    } | stream_changes]
    if extra_stream:
        streams.append({"codec_type": "audio", "codec_name": "opus"})
    payload = {
        "streams": streams,
        "packets": [{"pts": pts, "duration": 33} for pts in timestamps],
        "format": {"duration": "3"} | format_changes,
    }
    monkeypatch.setattr(
        webm, "run_ffmpeg",
        lambda args, timeout: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    with pytest.raises(MediaPolicyError) as error:
        webm.inspect_video_sticker(source)
    assert error.value.code is MediaPolicyCode.INVALID_MEDIA


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (WEBM_HEADER[:8], MediaPolicyCode.INVALID_MEDIA),
        (WEBM_HEADER[:-1], MediaPolicyCode.INVALID_MEDIA),
        (WEBM_HEADER.replace(b"webm", b"fake"), MediaPolicyCode.INVALID_MEDIA),
        (WEBM_HEADER.ljust(webm.MAX_WEBM_STICKER_BYTES + 1, b"\0"),
         MediaPolicyCode.MEDIA_FILE_TOO_LARGE),
    ],
)
def test_rejects_invalid_ebml_before_starting_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, code: MediaPolicyCode,
) -> None:
    source = tmp_path / "quarantine.upload"
    source.write_bytes(payload)

    def unexpected_probe(*args: object) -> None:
        pytest.fail("invalid container reached FFprobe")

    monkeypatch.setattr(webm, "run_ffmpeg", unexpected_probe)
    with pytest.raises(MediaPolicyError) as error:
        webm.inspect_video_sticker(source)
    assert error.value.code is code


def test_rejects_partial_probe_even_with_success_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """частично прочитанный контейнер не проходит проверку по выжившим пакетам"""

    source = tmp_path / "quarantine.upload"
    source.write_bytes(WEBM_HEADER)
    monkeypatch.setattr(
        webm, "run_ffmpeg",
        lambda args, timeout: subprocess.CompletedProcess(args, 0, "{}", "File ended prematurely"),
    )
    with pytest.raises(MediaPolicyError, match="FFprobe обнаружил ошибку"):
        webm.inspect_video_sticker(source)


@pytest.mark.skipif(not WEBM_TOOLS_AVAILABLE, reason="FFmpeg, FFprobe and prlimit are required")
def test_live_webm_uses_packet_duration(tmp_path: Path) -> None:
    """вычислить длительность короткого WebM без поля Duration в контейнере"""

    source = tmp_path / "live.webm"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=512x128:r=10:d=1",
            "-c:v", "libvpx-vp9", "-live", "1", str(source),
        ],
        check=True, timeout=20,
    )
    assert webm.inspect_video_sticker(source).duration == 1
    assert webm.transcode_video_sticker(source, tmp_path / "out.webm", tmp_path / "out.webp") == 1


@pytest.mark.skipif(not WEBM_TOOLS_AVAILABLE, reason="FFmpeg, FFprobe and prlimit are required")
def test_declared_alpha_requires_decoded_alpha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """отклонить WebM с AlphaMode, если в кадрах отсутствует альфа-канал"""

    source = tmp_path / "opaque-with-alpha-tag.webm"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=512x128:r=10:d=1",
            "-c:v", "libvpx-vp9", str(source),
        ],
        check=True, timeout=20,
    )
    original = webm.inspect_video_sticker(source)
    assert not original.alpha
    # подменяем только результат проверки AlphaMode - декодер читает настоящий VP9
    monkeypatch.setattr(
        webm, "inspect_video_sticker", lambda *args, **kwargs: replace(original, alpha=True),
    )
    preview = tmp_path / "preview.webp"
    with pytest.raises(MediaPolicyError, match="FFmpeg/FFprobe не смог обработать медиа"):
        webm.transcode_video_sticker(source, tmp_path / "out.webm", preview)
    assert not preview.exists()


def test_webm_transcode_preserves_alpha_and_first_frame(
    tmp_path: Path, webm_payload: bytes,
) -> None:
    source = tmp_path / "quarantine.upload"
    target = tmp_path / "canonical.webm"
    preview = tmp_path / "preview.webp"
    source.write_bytes(webm_payload)
    original = webm.inspect_video_sticker(source)
    assert original.alpha and original.duration == 2 and len(original.timestamps) == 24
    assert webm.transcode_video_sticker(source, target, preview) == 2
    result = webm.inspect_video_sticker(target)
    assert result == original
    assert result.timestamps[1] == Fraction(83, 1000)
    assert b"discard this metadata" not in target.read_bytes()
    assert target.stat().st_size <= webm.MAX_WEBM_STICKER_BYTES

    # принудительно декодируем WebM через libvpx, включая последний кадр
    rgba = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-c:v", "libvpx-vp9", "-i", str(target),
            "-vf", "alphaextract", "-f", "rawvideo", "pipe:1",
        ],
        check=True, capture_output=True, timeout=10,
    ).stdout
    pixels_per_frame = 512 * 128
    assert len(rgba) == 24 * pixels_per_frame
    assert rgba[0] == 0 and 120 <= rgba[50 * 512 + 60] <= 130
    assert 120 <= rgba[-pixels_per_frame + 50 * 512 + 150] <= 130
    with Image.open(preview) as image:
        assert image.format == "WEBP" and image.n_frames == 1 and image.size == (512, 128)
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((50, 50))[3] == rgba[50 * 512 + 50]


@pytest.mark.skipif(not WEBM_TOOLS_AVAILABLE, reason="FFmpeg, FFprobe and prlimit are required")
@pytest.mark.parametrize("variable", [False, True], ids=["30fps", "variable"])
def test_webm_accepts_silent_portrait_without_alpha(tmp_path: Path, variable: bool) -> None:
    source = tmp_path / "source.webm"
    target = tmp_path / "canonical.webm"
    preview = tmp_path / "preview.webp"
    timing = (
        ["-vf", "select=not(mod(n\\,2))+not(mod(n\\,3))", "-fps_mode", "passthrough"]
        if variable else []
    )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=128x512:r=30:d=3",
            *timing, "-c:v", "libvpx-vp9", "-threads:v", "2", str(source),
        ],
        check=True, timeout=20,
    )
    original = webm.inspect_video_sticker(source)
    assert not original.alpha
    if variable:
        assert 2 < original.duration < 3 and len(original.timestamps) == 60
    else:
        assert original.duration == 3 and len(original.timestamps) == 90
    assert webm.transcode_video_sticker(source, target, preview) == 3
    assert webm.inspect_video_sticker(target) == original
    with Image.open(preview) as image:
        assert image.n_frames == 1 and image.size == (128, 512) and image.mode == "RGB"
