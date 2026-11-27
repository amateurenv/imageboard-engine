"""проверки серверных пределов MP4 и отдельного типа видеокружка"""

import json
import subprocess
from pathlib import Path

import pytest

from app.services import video_processing
from app.services.media_policy import MediaPolicyCode, MediaPolicyError, MediaType
from app.services.video_processing import VideoInspection, VideoProcessor

MP4_BOXES = b"\0\0\0\x10ftypisom\0\0\0\0\0\0\0\x08moov\0\0\0\x08mdat"


def stub_probe(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """вернуть минимальные поля, которые читает обработчик"""

    payload = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2"},
        "streams": [
            {
                "codec_type": "video", "codec_name": "h264", "width": 320, "height": 320,
                "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "duration": "2",
                "nb_read_packets": "60", **overrides,
            },
        ],
    }
    monkeypatch.setattr(
        video_processing, "run_ffmpeg",
        lambda args, timeout: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )


@pytest.mark.parametrize("kind", [MediaType.VIDEO, MediaType.VIDEO_NOTE])
def test_square_mp4_can_be_inspected_for_either_trusted_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: MediaType,
) -> None:
    source = tmp_path / "untrusted.gif"
    source.write_bytes(MP4_BOXES)
    stub_probe(monkeypatch)
    assert VideoProcessor().inspect(source, kind) == VideoInspection(2, None, None)


@pytest.mark.parametrize(
    ("kind", "params", "expected"),
    [
        (MediaType.VIDEO, {"width": 1921}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"height": 1081, "width": 1920}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"avg_frame_rate": "30/0"}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"r_frame_rate": "30001/1000"}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"nb_read_packets": "61"}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"duration": "120.01"}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"sample_aspect_ratio": "4:3"}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO, {"codec_name": "mpeg4"}, MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE),
        (MediaType.VIDEO, {"disposition": {"attached_pic": 1}},
         MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE),
        (MediaType.VIDEO_NOTE, {"width": 328}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO_NOTE, {"width": 642, "height": 642}, MediaPolicyCode.INVALID_MEDIA),
        (MediaType.VIDEO_NOTE, {"duration": "60.01"}, MediaPolicyCode.INVALID_MEDIA),
    ],
)
def test_inspection_rejects_unsupported_or_over_budget_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: MediaType,
    params: dict[str, object], expected: MediaPolicyCode,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(MP4_BOXES)
    stub_probe(monkeypatch, **params)
    with pytest.raises(MediaPolicyError) as error:
        VideoProcessor().inspect(source, kind)
    assert error.value.code is expected


def test_truncated_mp4_does_not_reach_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(MP4_BOXES[:-1])

    def unexpected_probe(*args: object) -> None:
        pytest.fail("truncated box reached FFprobe")

    monkeypatch.setattr(video_processing, "run_ffmpeg", unexpected_probe)
    with pytest.raises(MediaPolicyError) as error:
        VideoProcessor().inspect(source, MediaType.VIDEO)
    assert error.value.code is MediaPolicyCode.INVALID_MEDIA
