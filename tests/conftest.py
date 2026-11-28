"""общие тестовые данные для статичных и анимированных media"""

import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


@pytest.fixture
def tgs_document() -> dict:
    """создать анимацию с движущимся полупрозрачным кругом"""

    return {
        "v": "5.7.4", "fr": 60, "ip": 0, "op": 120, "w": 512, "h": 512,
        "tgs": 1, "nm": "removed metadata", "ddd": 0, "assets": [],
        "layers": [{
            "ddd": 0, "ind": 1, "ty": 4, "sr": 1, "ip": 0, "op": 120, "st": 0, "bm": 0,
            "ks": {
                "o": {"a": 0, "k": 50},
                "r": {"a": 0, "k": 0},
                "p": {"a": 1, "k": [
                    {"t": 0, "s": [256, 256, 0], "e": [320, 256, 0],
                     "i": {"x": 0.5, "y": 0.5}, "o": {"x": 0.5, "y": 0.5}},
                    {"t": 120, "s": [320, 256, 0]},
                ]},
                "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "shapes": [
                {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "d": 1},
                {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}, "o": {"a": 0, "k": 100}, "r": 1},
            ],
        }],
    }


@pytest.fixture
def tgs_payload(tgs_document: dict) -> bytes:
    return gzip.compress(json.dumps(tgs_document).encode("utf-8"), mtime=12345)


@pytest.fixture
def webm_payload(tmp_path: Path) -> bytes:
    """закодировать движение полупрозрачной фигуры на прозрачном фоне"""

    if not all(shutil.which(binary) for binary in ("ffmpeg", "ffprobe", "/usr/bin/prlimit")):
        pytest.skip("FFmpeg, FFprobe and /usr/bin/prlimit are required")
    frames = []
    for index in range(24):
        image = Image.new("RGBA", (512, 128))
        ImageDraw.Draw(image).rectangle((40 + index * 4, 20, 80 + index * 4, 100), fill=(
            255, 0, 0, 125,
        ))
        frames.append(image.tobytes())
    output = tmp_path / "input-sticker.webm"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgba",
            "-video_size", "512x128", "-framerate", "12", "-i", "pipe:0",
            "-metadata", "title=discard this metadata", "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p", "-threads:v", "2", "-lossless", "1", str(output),
        ],
        input=b"".join(frames), check=True, timeout=20,
    )
    return output.read_bytes()
