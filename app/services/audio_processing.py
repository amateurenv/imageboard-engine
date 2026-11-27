"""безопасная проверка и перекодирование audio через фиксированные FFmpeg команды"""

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError, MediaType
from app.services.media_storage import MediaStorage

PROBE_TIMEOUT_SECONDS = 10
TRANSCODE_TIMEOUT_SECONDS = 120
MAX_AUDIO_DURATION_SECONDS = 30 * 60
MAX_VOICE_DURATION_SECONDS = 10 * 60
MAX_SAMPLE_RATE = 48_000


@dataclass(frozen=True, slots=True)
class AudioInspection:
    """безопасные технические данные, полученные только из ffprobe"""

    media_type: MediaType
    source_mime_type: str
    duration_seconds: int
    channels: int
    sample_rate: int


class FFmpegMediaProcessor:
    """не принимать filename клиента и не вызывать shell при работе с media"""

    def __init__(self, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe") -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin

    def looks_like_audio_candidate(self, path: Path) -> bool:
        """решить, нужен ли ffprobe для container без доверия расширению"""

        with path.open("rb") as source:
            header = source.read(16)
        known_audio = (
            header.startswith((b"ID3", b"OggS"))
            or header.startswith(b"\xff\xfb")
            or header.startswith(b"\xff\xf3")
            or header.startswith(b"\xff\xf1")
            or header.startswith(b"\xff\xf9")
        )
        if known_audio:
            return True
        # generic isom/mp4 может быть video - без M4A brand это запрещённый container
        return len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {
            b"M4A ",
            b"M4B ",
        }

    def inspect(self, source_path: Path) -> AudioInspection:
        """разрешить только mp3 aac/m4a и ogg/opus после parser проверки"""

        payload = self._probe_json(source_path)
        streams = payload.get("streams")
        container = payload.get("format")
        if not isinstance(streams, list) or not isinstance(container, dict):
            raise MediaPolicy.invalid_media()
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        if len(audio_streams) != 1 or has_video:
            raise MediaPolicy.invalid_media("файл не является поддерживаемым audio")
        stream = audio_streams[0]
        codec = stream.get("codec_name")
        format_names = set(str(container.get("format_name", "")).split(","))
        duration_seconds = self._duration(container.get("duration"))
        channels = self._positive_int(stream.get("channels"))
        sample_rate = self._positive_int(stream.get("sample_rate"))
        if sample_rate > MAX_SAMPLE_RATE:
            raise MediaPolicy.invalid_media("частота audio превышает server-side лимит")
        if format_names == {"ogg"} and codec == "opus":
            media_type = MediaType.VOICE
            mime_type = "audio/ogg"
            if channels != 1 or duration_seconds > MAX_VOICE_DURATION_SECONDS:
                raise MediaPolicy.invalid_media("voice не соответствует server-side лимиту")
        elif codec == "mp3" and "mp3" in format_names:
            media_type = MediaType.AUDIO
            mime_type = "audio/mpeg"
            if channels > 2 or duration_seconds > MAX_AUDIO_DURATION_SECONDS:
                raise MediaPolicy.invalid_media("audio не соответствует server-side лимиту")
        elif codec == "aac" and (
            "aac" in format_names or "mov" in format_names or "mp4" in format_names
        ):
            media_type = MediaType.AUDIO
            mime_type = "audio/mp4"
            if channels > 2 or duration_seconds > MAX_AUDIO_DURATION_SECONDS:
                raise MediaPolicy.invalid_media("audio не соответствует server-side лимиту")
        else:
            raise MediaPolicy.invalid_media("container или codec audio не поддерживается")
        return AudioInspection(media_type, mime_type, duration_seconds, channels, sample_rate)

    def transcode(
        self, source_path: Path, storage: MediaStorage, media_id: int, inspection: AudioInspection
    ) -> tuple[str, str]:
        """полностью декодировать controlled source и atomically сохранить canonical derivative"""

        if inspection.media_type is MediaType.AUDIO:
            relative_path, mime_type = f"audio/{media_id}.m4a", "audio/mp4"
            codec_args = [
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
                "-movflags", "+faststart", "-f", "ipod",
            ]
        else:
            relative_path, mime_type = f"voice/{media_id}.ogg", "audio/ogg"
            codec_args = ["-c:a", "libopus", "-b:a", "64k", "-ac", "1", "-ar", "48000", "-f", "ogg"]
        temporary_path = storage.temporary_path(relative_path)
        try:
            self._run(
                [
                    self._ffmpeg_bin, "-v", "error", "-nostdin", "-y", "-i", str(source_path),
                    "-map", "0:a:0", "-vn", "-sn", "-dn", *codec_args, str(temporary_path),
                ],
                TRANSCODE_TIMEOUT_SECONDS,
            )
            if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
                raise MediaPolicy.invalid_media("FFmpeg не создал derivative audio")
            storage.replace_from_temporary(temporary_path, relative_path)
            return relative_path, mime_type
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _probe_json(self, source_path: Path) -> dict[str, object]:
        """получить JSON parser без stderr в ошибке API и без shell"""

        completed = self._run(
            [
                self._ffprobe_bin, "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(source_path),
            ],
            PROBE_TIMEOUT_SECONDS,
        )
        try:
            decoded = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise MediaPolicy.invalid_media("FFprobe вернул некорректные метаданные") from error
        if not isinstance(decoded, dict):
            raise MediaPolicy.invalid_media()
        return decoded

    @staticmethod
    def _duration(value: object) -> int:
        try:
            duration = float(str(value))
        except (TypeError, ValueError) as error:
            raise MediaPolicy.invalid_media("длительность audio не определена") from error
        if not math.isfinite(duration) or duration <= 0:
            raise MediaPolicy.invalid_media("длительность audio не определена")
        return math.ceil(duration)

    @staticmethod
    def _positive_int(value: object) -> int:
        try:
            number = int(str(value))
        except (TypeError, ValueError) as error:
            raise MediaPolicy.invalid_media("технические параметры audio не определены") from error
        if number <= 0:
            raise MediaPolicy.invalid_media("технические параметры audio не определены")
        return number

    @staticmethod
    def _run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        """выполнить заранее собранный список аргументов без shell и с timeout"""

        try:
            completed = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
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
            raise MediaPolicy.invalid_media("превышено время безопасной обработки audio") from error
        if completed.returncode != 0:
            raise MediaPolicy.invalid_media("FFmpeg/FFprobe не смог обработать audio")
        return completed
