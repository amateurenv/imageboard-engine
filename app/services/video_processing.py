"""проверка MP4 и создание канонического видео со статичным preview"""

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from app.services.ffmpeg import (
    PROBE_TIMEOUT_SECONDS,
    TRANSCODE_TIMEOUT_SECONDS,
    bounded_output,
    decode_probe,
    positive_int,
    positive_number,
    run_ffmpeg,
)
from app.services.media_policy import (
    MEBIBYTE,
    MediaPolicy,
    MediaPolicyCode,
    MediaPolicyError,
    MediaType,
)
from app.services.media_quota import MP4_WORK_BYTES, PREVIEW_BYTES

VIDEO_KINDS = frozenset({MediaType.VIDEO, MediaType.VIDEO_NOTE})
MAX_VIDEO_DURATION = 120
MAX_VIDEO_NOTE_DURATION = 60
MAX_VIDEO_NOTE_SIDE = 640
MAX_VIDEO_FPS = 30
VIDEO_SAMPLE_RATE = 48_000
MAX_VIDEO_BYTES = 20 * MEBIBYTE
VIDEO_MIME = "video/mp4"
MP4_BRANDS = frozenset({b"isom", b"mp41", b"mp42", b"avc1", b"iso2", b"iso4", b"iso5", b"iso6"})
# разрешаем демультиплексору читать только локальный вход и отключаем внешние data references
MP4_INPUT = [
    "-max_alloc", str(64 * MEBIBYTE), "-protocol_whitelist", "file", "-f", "mov",
    "-enable_drefs", "0", "-use_absolute_path", "0", "-max_streams", "8",
    "-threads", "2", "-max_pixels", str(2048 * 1152),
]


@dataclass(frozen=True, slots=True)
class VideoInspection:
    """проверенные параметры видео для очереди"""

    duration_seconds: int
    channels: int | None
    sample_rate: int | None
    audio_duration: float | None = None


def looks_like_mp4_video(path: Path) -> bool:
    """распознать поддерживаемую ветку MP4 по содержимому"""

    with path.open("rb") as source:
        header = source.read(12)
    return header[4:8] == b"ftyp" and header[8:12] in MP4_BRANDS


def _validate_mp4_boxes(path: Path) -> None:
    """проверить границы верхних MP4 boxes до передачи файла демультиплексору"""

    size = path.stat().st_size
    offset = 0
    boxes: set[bytes] = set()
    with path.open("rb") as source:
        for _ in range(4096):
            if offset == size:
                if {b"ftyp", b"moov", b"mdat"} <= boxes:
                    return
                break
            source.seek(offset)
            header = source.read(8)
            if len(header) != 8:
                break
            length = int.from_bytes(header[:4], "big")
            box_type = header[4:]
            header_size = 8
            if length == 1:
                length = int.from_bytes(source.read(8), "big")
                header_size = 16
            elif length == 0:
                length = size - offset
            if length < header_size or offset + length > size:
                break
            boxes.add(box_type)
            offset += length
    raise MediaPolicy.invalid_media("структура MP4 повреждена или слишком сложна")


def _frame_rate(value: object) -> Fraction:
    """сравнить частоту кадров без округления"""

    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as error:
        raise MediaPolicy.invalid_media("частота кадров MP4 не определена") from error
    if not 0 < rate <= MAX_VIDEO_FPS:
        raise MediaPolicy.invalid_media("частота кадров MP4 превышает server-side лимит")
    return rate


def _unsupported_video() -> MediaPolicyError:
    return MediaPolicyError(
        code=MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE,
        message="поддерживается MP4 с H.264 и необязательной дорожкой AAC",
        http_status=415,
    )


class VideoProcessor:
    """одинаково проверять видео в upload и worker, тип кружка задаёт доверенный код"""

    def __init__(self, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe") -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin

    def inspect(self, path: Path, kind: MediaType) -> VideoInspection:
        """проверить container, дорожки и бюджеты декодирования"""

        if kind not in VIDEO_KINDS or not looks_like_mp4_video(path):
            raise _unsupported_video()
        _validate_mp4_boxes(path)
        completed = run_ffmpeg(
            [
                self._ffprobe_bin, "-v", "quiet", *MP4_INPUT,
                "-count_packets", "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,width,height,"
                "sample_aspect_ratio,avg_frame_rate,r_frame_rate,nb_read_packets,"
                "duration,channels,sample_rate:stream_disposition=attached_pic",
                "-of", "json", str(path),
            ],
            PROBE_TIMEOUT_SECONDS,
        )
        payload = decode_probe(completed.stdout)
        streams = payload.get("streams")
        container = payload.get("format")
        if (
            not isinstance(streams, list)
            or any(not isinstance(stream, dict) for stream in streams)
            or not isinstance(container, dict)
        ):
            raise MediaPolicy.invalid_media()
        if "mp4" not in str(container.get("format_name", "")).split(","):
            raise _unsupported_video()
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if len(video) != 1 or len(audio) > 1 or len(streams) != len(video) + len(audio):
            raise _unsupported_video()
        stream = video[0]
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict):
            raise MediaPolicy.invalid_media()
        if stream.get("codec_name") != "h264" or disposition.get("attached_pic"):
            raise _unsupported_video()

        duration = positive_number(container.get("duration"), "duration")
        video_duration = positive_number(stream.get("duration"), "video duration")
        limit = MAX_VIDEO_NOTE_DURATION if kind is MediaType.VIDEO_NOTE else MAX_VIDEO_DURATION
        if max(duration, video_duration) > limit:
            raise MediaPolicy.invalid_media("длительность MP4 превышает server-side лимит")
        width = positive_int(stream.get("width"), "width")
        height = positive_int(stream.get("height"), "height")
        if min(width, height) < 2 or max(width, height) > 1920 or min(width, height) > 1080:
            raise MediaPolicy.invalid_media("разрешение MP4 превышает server-side лимит")
        if stream.get("sample_aspect_ratio") not in {None, "N/A", "1:1"}:
            raise MediaPolicy.invalid_media("MP4 должен использовать квадратные пиксели")
        if kind is MediaType.VIDEO_NOTE and (width != height or width > MAX_VIDEO_NOTE_SIDE):
            raise MediaPolicy.invalid_media("видеокружок должен быть квадратным и до 640 px")
        _frame_rate(stream.get("avg_frame_rate"))
        _frame_rate(stream.get("r_frame_rate"))
        frames = positive_int(stream.get("nb_read_packets"), "video packets")
        if frames > min(limit * MAX_VIDEO_FPS, math.ceil(video_duration * MAX_VIDEO_FPS)):
            raise MediaPolicy.invalid_media("число кадров MP4 превышает server-side лимит")
        if not audio:
            return VideoInspection(math.ceil(max(duration, video_duration)), None, None)
        audio_stream = audio[0]
        if audio_stream.get("codec_name") != "aac":
            raise _unsupported_video()
        channels = positive_int(audio_stream.get("channels"), "channels")
        sample_rate = positive_int(audio_stream.get("sample_rate"), "sample rate")
        audio_duration = positive_number(audio_stream.get("duration"), "audio duration")
        if channels > 2 or sample_rate > VIDEO_SAMPLE_RATE or audio_duration > limit:
            raise MediaPolicy.invalid_media("аудиодорожка MP4 превышает server-side лимит")
        return VideoInspection(
            math.ceil(max(duration, video_duration, audio_duration)), channels, sample_rate,
            audio_duration,
        )

    def transcode(
        self, source: Path, video: Path, preview: Path, kind: MediaType,
    ) -> VideoInspection:
        """перекодировать проверенный источник и вернуть фактические параметры готового MP4"""

        original = self.inspect(source, kind)
        audio_filter = []
        if original.audio_duration is not None:
            # декодер AAC может добавить padding за пределами заявленной длины дорожки
            samples = round(original.audio_duration * VIDEO_SAMPLE_RATE)
            audio_filter = ["-af", f"aresample={VIDEO_SAMPLE_RATE},atrim=end_sample={samples}"]
        run_ffmpeg(
            bounded_output([
                self._ffmpeg_bin, "-v", "error", "-nostdin", "-y", "-xerror",
                "-abort_on", "empty_output+empty_output_stream", "-filter_threads", "1",
                *MP4_INPUT, "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
                "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,sidedata=mode=delete",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-profile:v", "main", "-preset", "veryfast", "-threads:v", "2",
                "-b:v", "950k", "-maxrate", "1M", "-bufsize", "2M",
                "-bsf:v", "filter_units=remove_types=6",
                *audio_filter, "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k",
                "-fs", str(MAX_VIDEO_BYTES + 1),
                "-movflags", "+faststart", "-f", "mp4", str(video),
            ], MP4_WORK_BYTES),
            TRANSCODE_TIMEOUT_SECONDS,
        )
        if not video.is_file() or not 0 < video.stat().st_size <= MAX_VIDEO_BYTES:
            raise MediaPolicy.invalid_media("канонический MP4 не уложился в лимит")
        result = self.inspect(video, kind)
        expected_sample_rate = VIDEO_SAMPLE_RATE if original.channels is not None else None
        if result.channels != original.channels or result.sample_rate != expected_sample_rate:
            raise MediaPolicy.invalid_media("канонический MP4 потерял параметры аудиодорожки")
        run_ffmpeg(
            bounded_output([
                self._ffmpeg_bin, "-v", "error", "-nostdin", "-y", "-xerror",
                "-abort_on", "empty_output", "-filter_threads", "1",
                *MP4_INPUT, "-i", str(video), "-map", "0:v:0", "-frames:v", "1",
                "-vf", r"scale=min(iw\,320):min(ih\,180):force_original_aspect_ratio=decrease",
                "-map_metadata", "-1", "-c:v", "libwebp", "-threads:v", "1",
                "-f", "image2", "-update", "1", str(preview),
            ], PREVIEW_BYTES),
            PROBE_TIMEOUT_SECONDS,
        )
        if not preview.is_file() or preview.stat().st_size == 0:
            raise MediaPolicy.invalid_media("FFmpeg не создал preview видео")
        return result
