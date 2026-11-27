"""проверка audio policy через mock FFprobe/FFmpeg без системного бинарника"""

import json
import subprocess
from pathlib import Path

import pytest

from app.services.audio_processing import FFmpegMediaProcessor
from app.services.media_policy import MediaPolicyCode, MediaPolicyError, MediaType
from app.services.media_storage import MediaStorage


def probe_result(
    *,
    codec: str = "mp3",
    format_name: str = "mp3",
    duration: str = "12.1",
    channels: int = 2,
    sample_rate: str = "48000",
    video: bool = False,
) -> subprocess.CompletedProcess[str]:
    """собрать безопасный ffprobe JSON без обращения к реальному decoder"""

    streams: list[dict[str, object]] = [
        {
            "codec_type": "audio",
            "codec_name": codec,
            "channels": channels,
            "sample_rate": sample_rate,
        }
    ]
    if video:
        streams.append({"codec_type": "video", "codec_name": "h264"})
    payload = {
        "streams": streams,
        "format": {"format_name": format_name, "duration": duration},
    }
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


@pytest.mark.parametrize(
    ("codec", "format_name", "expected_type", "expected_mime", "channels"),
    [
        ("mp3", "mp3", MediaType.AUDIO, "audio/mpeg", 2),
        ("aac", "mov,mp4,m4a,3gp,3g2,mj2", MediaType.AUDIO, "audio/mp4", 2),
        ("opus", "ogg", MediaType.VOICE, "audio/ogg", 1),
    ],
)
def test_ffprobe_accepts_only_supported_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    codec: str,
    format_name: str,
    expected_type: MediaType,
    expected_mime: str,
    channels: int,
) -> None:
    """тип определяется codec и container, а не filename или client MIME"""

    processor = FFmpegMediaProcessor()
    monkeypatch.setattr(
        processor,
        "_run",
        lambda arguments, timeout: probe_result(
            codec=codec, format_name=format_name, channels=channels
        ),
    )
    source = tmp_path / "not-trusted.bin"
    source.write_bytes(b"OggS" if codec == "opus" else b"ID3")

    inspected = processor.inspect(source)

    assert inspected.media_type is expected_type
    assert inspected.source_mime_type == expected_mime


@pytest.mark.parametrize(
    "kwargs",
    [
        {"codec": "vorbis", "format_name": "ogg"},
        {"codec": "pcm_s16le", "format_name": "wav"},
        {"codec": "flac", "format_name": "flac"},
        {"codec": "aac", "format_name": "mov,mp4", "video": True},
        {"codec": "mp3", "format_name": "mp3", "duration": "1801"},
        {"codec": "opus", "format_name": "ogg", "duration": "601"},
        {"codec": "mp3", "format_name": "mp3", "sample_rate": "96000"},
        {"codec": "opus", "format_name": "ogg", "channels": 2},
    ],
)
def test_ffprobe_rejects_untrusted_or_over_limit_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kwargs: dict[str, object]
) -> None:
    """codec duration channels и sample rate ограничиваются до worker"""

    processor = FFmpegMediaProcessor()
    monkeypatch.setattr(processor, "_run", lambda arguments, timeout: probe_result(**kwargs))
    source = tmp_path / "untrusted"
    source.write_bytes(b"OggS")

    with pytest.raises(MediaPolicyError) as error:
        processor.inspect(source)
    assert error.value.code is MediaPolicyCode.INVALID_MEDIA


def test_transcode_uses_controlled_paths_and_never_shells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ffmpeg получает только список аргументов и создаёт canonical audio derivative"""

    storage = MediaStorage(tmp_path / "media")
    source = storage.resolve("quarantine/source")
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(b"ID3")
    processor = FFmpegMediaProcessor()
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(b"canonical-audio")
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(processor, "_run", fake_run)
    from app.services.audio_processing import AudioInspection

    path, mime_type = processor.transcode(
        source,
        storage,
        9,
        AudioInspection(MediaType.AUDIO, "audio/mpeg", 2, 2, 48_000),
    )

    assert (path, mime_type) == ("audio/9.m4a", "audio/mp4")
    assert storage.resolve(path).is_file()
    assert calls[0][0] == "ffmpeg"
    assert "shell" not in " ".join(calls[0])
