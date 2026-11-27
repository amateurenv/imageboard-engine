"""запуск FFmpeg без shell и с ограничением времени"""

import json
import math
import subprocess

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError

PROBE_TIMEOUT_SECONDS = 10
TRANSCODE_TIMEOUT_SECONDS = 120


def run_ffmpeg(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """выполнить фиксированный список аргументов и скрыть stderr от клиента"""

    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise MediaPolicyError(
            code=MediaPolicyCode.MEDIA_PROCESSING_UNAVAILABLE,
            message="FFmpeg/FFprobe не установлен на сервере",
            http_status=503,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise MediaPolicy.invalid_media("превышено время безопасной обработки медиа") from error
    if completed.returncode != 0:
        raise MediaPolicy.invalid_media("FFmpeg/FFprobe не смог обработать медиа")
    return completed


def decode_probe(stdout: str) -> dict[str, object]:
    """разобрать небольшой JSON с техническими параметрами"""

    try:
        decoded = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise MediaPolicy.invalid_media("FFprobe вернул некорректные метаданные") from error
    if not isinstance(decoded, dict):
        raise MediaPolicy.invalid_media()
    return decoded


def positive_number(value: object, label: str) -> float:
    """принять только конечное положительное число из ffprobe"""

    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise MediaPolicy.invalid_media(f"не определён параметр медиа: {label}") from error
    if not math.isfinite(number) or number <= 0:
        raise MediaPolicy.invalid_media(f"не определён параметр медиа: {label}")
    return number


def positive_int(value: object, label: str) -> int:
    """принять положительное целое число без округления"""

    try:
        number = int(str(value))
    except (TypeError, ValueError) as error:
        raise MediaPolicy.invalid_media(f"не определён параметр медиа: {label}") from error
    if number <= 0:
        raise MediaPolicy.invalid_media(f"не определён параметр медиа: {label}")
    return number
