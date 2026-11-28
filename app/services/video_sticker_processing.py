"""проверка и перекодирование WebM-стикеров с сохранением прозрачности"""

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from app.services.ffmpeg import PROBE_TIMEOUT_SECONDS, decode_probe, positive_int, run_ffmpeg
from app.services.media_policy import MediaPolicy, MediaPolicyCode, MediaPolicyError
from app.services.sticker_processing import STICKER_SIDE

WEBM_STICKER_MIME = "video/webm"
EBML_SIGNATURE = b"\x1a\x45\xdf\xa3"
MAX_WEBM_STICKER_BYTES = 256 * 1024
WEBM_TRANSCODE_SIZE_LIMIT = 2 * MAX_WEBM_STICKER_BYTES
MAX_DURATION = 3
MAX_FPS = 30
MAX_FRAMES = MAX_DURATION * MAX_FPS
WEBM_MUXER_TICK = Fraction(1, 1000)
ALLOCATION_LIMIT = str(16 * 1024 * 1024)
WEBM_INPUT = ["-protocol_whitelist", "file", "-f", "matroska"]
# ограничиваем весь процесс, включая память, выделенную внутри libvpx
PROCESS_LIMITS = [
    "/usr/bin/prlimit", "--as=805306368", "--cpu=12", "--fsize=2097152", "--core=0", "--",
]


@dataclass(frozen=True, slots=True)
class VideoStickerInspection:
    """проверенные параметры WebM до и после перекодирования"""

    width: int
    height: int
    duration: Fraction
    timestamps: tuple[Fraction, ...]
    alpha: bool


def check_webm_sticker_size(size: int) -> None:
    """ограничить размер WebM до записи и запуска ffprobe"""

    if size > MAX_WEBM_STICKER_BYTES:
        raise MediaPolicyError(
            code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
            message="размер WebM-стикера больше 256 КиБ",
            http_status=413,
            limit_bytes=MAX_WEBM_STICKER_BYTES,
        )


def _ebml_number(data: bytes, offset: int, *, element_id: bool = False) -> tuple[int, int]:
    """прочитать переменную длину EBML без выхода за границы файла"""

    if offset >= len(data) or not data[offset]:
        raise MediaPolicy.invalid_media("некорректный заголовок EBML")
    width = 9 - data[offset].bit_length()
    end = offset + width
    if end > len(data) or (element_id and width > 4):
        raise MediaPolicy.invalid_media("некорректный заголовок EBML")
    value = int.from_bytes(data[offset:end], "big")
    if not element_id:
        value ^= 1 << (7 * width)
        if value == (1 << (7 * width)) - 1:
            value = -1
    return value, end


def _validate_webm_header(data: bytes) -> None:
    """отличить WebM от общего Matroska и проверить границу сегмента"""

    if not data.startswith(EBML_SIGNATURE):
        raise MediaPolicy.invalid_media("отсутствует заголовок WebM")
    size, offset = _ebml_number(data, len(EBML_SIGNATURE))
    header_end = offset + size
    if size <= 0 or header_end > len(data):
        raise MediaPolicy.invalid_media("обрезан заголовок WebM")
    doc_type = None
    while offset < header_end:
        element, offset = _ebml_number(data, offset, element_id=True)
        length, start = _ebml_number(data, offset)
        offset = start + length
        if length < 0 or offset > header_end:
            raise MediaPolicy.invalid_media("повреждён заголовок WebM")
        if element == 0x4282:
            if doc_type is not None:
                raise MediaPolicy.invalid_media("повторяется DocType WebM")
            doc_type = data[start:offset]
    if doc_type != b"webm":
        raise MediaPolicy.invalid_media("стикер должен использовать контейнер WebM")
    segment, offset = _ebml_number(data, header_end, element_id=True)
    size, offset = _ebml_number(data, offset)
    if segment != 0x18538067 or offset >= len(data) or size not in {-1, len(data) - offset}:
        raise MediaPolicy.invalid_media("повреждён сегмент WebM")


def _fraction(value: object) -> Fraction:
    """прочитать время и частоту без округления к целому"""

    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as error:
        raise MediaPolicy.invalid_media("некорректное время кадра WebM") from error


def inspect_video_sticker(path: Path, *, ffprobe_bin: str = "ffprobe") -> VideoStickerInspection:
    """проверить размер, дорожки, кадры и временную шкалу WebM"""

    with path.open("rb") as source:
        data = source.read(MAX_WEBM_STICKER_BYTES + 1)
    check_webm_sticker_size(len(data))
    _validate_webm_header(data)
    probe = run_ffmpeg(
        [
            *PROCESS_LIMITS, ffprobe_bin, "-v", "error", "-max_alloc", ALLOCATION_LIMIT,
            *WEBM_INPUT,
            "-read_intervals", f"%+#{MAX_FRAMES + 1}", "-show_packets",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,"
            "time_base,r_frame_rate,avg_frame_rate,sample_aspect_ratio:stream_tags=alpha_mode:"
            "packet=pts,duration", "-of", "json", str(path),
        ],
        PROBE_TIMEOUT_SECONDS,
    )
    # demuxer может сообщить об обрыве потока и вернуть частичный JSON с кодом 0
    if probe.stderr:
        raise MediaPolicy.invalid_media("FFprobe обнаружил ошибку в потоке WebM")
    payload = decode_probe(probe.stdout)
    streams, container, packets = (payload.get(key) for key in ("streams", "format", "packets"))
    if (
        not isinstance(streams, list) or len(streams) != 1
        or not isinstance(streams[0], dict) or not isinstance(container, dict)
    ):
        raise MediaPolicy.invalid_media("WebM-стикеру нужна одна дорожка VP9 без звука")
    stream = streams[0]
    if stream.get("codec_type") != "video" or stream.get("codec_name") != "vp9":
        raise MediaPolicy.invalid_media("WebM-стикеру нужна одна дорожка VP9 без звука")
    width = positive_int(stream.get("width"), "width")
    height = positive_int(stream.get("height"), "height")
    if max(width, height) != STICKER_SIDE or stream.get("sample_aspect_ratio") not in {
        None, "N/A", "1:1",
    }:
        raise MediaPolicy.invalid_media("одна сторона WebM должна быть ровно 512 px")
    if not isinstance(packets, list) or not 0 < len(packets) <= MAX_FRAMES:
        raise MediaPolicy.invalid_media("число кадров WebM превышает лимит")
    rate = _fraction(stream.get("r_frame_rate"))
    average_rate = _fraction(stream.get("avg_frame_rate"))
    # r_frame_rate может быть оценкой общего делителя интервалов при переменной частоте
    if rate <= 0 or not 0 <= average_rate <= MAX_FPS:
        raise MediaPolicy.invalid_media("частота кадров WebM превышает 30 FPS")
    time_base = _fraction(stream.get("time_base"))
    if time_base <= 0:
        raise MediaPolicy.invalid_media("некорректная временная шкала WebM")
    tolerance = min(time_base, WEBM_MUXER_TICK)
    default_duration = Fraction(1) / (average_rate or min(rate, Fraction(MAX_FPS)))
    timestamps: list[Fraction] = []
    end_time = Fraction(0)
    for packet in packets:
        if not isinstance(packet, dict):
            raise MediaPolicy.invalid_media()
        timestamp = _fraction(packet.get("pts")) * time_base
        # допускаем округление на один тик при 30 кадрах/с
        if timestamp < 0 or (
            timestamps and timestamp - timestamps[-1] < Fraction(1, MAX_FPS) - tolerance
        ):
            raise MediaPolicy.invalid_media("некорректные интервалы кадров WebM")
        timestamps.append(timestamp)
        duration_ticks = packet.get("duration")
        frame_duration = (
            _fraction(duration_ticks) * time_base
            if duration_ticks is not None else default_duration
        )
        if frame_duration <= 0:
            raise MediaPolicy.invalid_media("некорректная длительность кадра WebM")
        end_time = max(end_time, timestamp + frame_duration)
    declared_duration = container.get("duration")
    duration = end_time
    if declared_duration not in (None, "N/A"):
        declared = _fraction(declared_duration)
        if declared <= 0:
            raise MediaPolicy.invalid_media("некорректная длительность WebM")
        duration = max(end_time, declared)
    # также ограничиваем среднюю частоту кадров по фактическому времени
    if duration > MAX_DURATION or len(timestamps) > (duration + tolerance) * MAX_FPS:
        raise MediaPolicy.invalid_media("длительность или частота кадров WebM превышает лимит")
    tags = stream.get("tags", {})
    if not isinstance(tags, dict):
        raise MediaPolicy.invalid_media()
    return VideoStickerInspection(
        width, height, duration, tuple(timestamps), str(tags.get("alpha_mode")) == "1",
    )


def transcode_video_sticker(
    source: Path, target: Path, preview: Path, *,
    ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe",
) -> int:
    """перекодировать VP9, проверить результат и сохранить статичный первый кадр"""

    original = inspect_video_sticker(source, ffprobe_bin=ffprobe_bin)
    ffmpeg = [
        *PROCESS_LIMITS, ffmpeg_bin, "-v", "error", "-nostdin", "-y", "-xerror",
        "-abort_on", "empty_output",
        "-max_alloc", ALLOCATION_LIMIT, "-filter_threads", "1", *WEBM_INPUT,
        # штатный декодер VP9 не возвращает альфа-канал из WebM BlockAdditional
        "-c:v", "libvpx-vp9", "-threads", "1", "-max_pixels", str(STICKER_SIDE ** 2),
        "-noautorotate",
    ]
    pixel_format = "yuva420p" if original.alpha else "yuv420p"
    for quality in ("32", "45", "55"):
        run_ffmpeg(
            [
                *ffmpeg, "-i", str(source), "-map", "0:v:0",
                "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
                "-vf", f"format={pixel_format},setsar=1,sidedata=mode=delete",
                "-fps_mode", "passthrough", "-enc_time_base", "1:1000",
                "-frames:v", str(MAX_FRAMES + 1), "-c:v", "libvpx-vp9", "-threads:v", "2",
                # запрещаем фильтрам подставить непрозрачную альфу вместо отсутствующей
                "-pix_fmt", "+yuva420p" if original.alpha else pixel_format,
                "-deadline", "good", "-cpu-used", "4", "-lag-in-frames", "0",
                "-auto-alt-ref", "0", "-crf", quality, "-b:v", "0",
                # при срабатывании -fs файл должен попасть в следующую попытку сжатия
                "-fs", str(WEBM_TRANSCODE_SIZE_LIMIT), "-f", "webm", str(target),
            ],
            20,
        )
        if 0 < target.stat().st_size <= MAX_WEBM_STICKER_BYTES:
            break
    else:
        raise MediaPolicy.invalid_media("канонический WebM не уложился в 256 КиБ")
    result = inspect_video_sticker(target, ffprobe_bin=ffprobe_bin)
    # сверяем кадры после перекодирования, включая обрезку по ограничению размера
    relative_timestamps = tuple(t - original.timestamps[0] for t in original.timestamps)
    if (
        (result.width, result.height) != (original.width, original.height)
        or result.alpha != original.alpha
        or len(result.timestamps) != len(relative_timestamps)
        or any(
            abs(actual - expected) > WEBM_MUXER_TICK / 2
            for actual, expected in zip(result.timestamps, relative_timestamps, strict=True)
        )
    ):
        raise MediaPolicy.invalid_media("канонический WebM потерял параметры исходного стикера")
    run_ffmpeg(
        [
            *ffmpeg, "-i", str(target), "-map", "0:v:0", "-frames:v", "1",
            "-map_metadata", "-1", "-c:v", "libwebp", "-lossless", "1",
            "-threads:v", "1", "-f", "image2", "-update", "1", str(preview),
        ],
        PROBE_TIMEOUT_SECONDS,
    )
    if not preview.is_file() or preview.stat().st_size == 0:
        raise MediaPolicy.invalid_media("FFmpeg не создал превью WebM")
    return math.ceil(result.duration)
