"""загрузка медиа до включения публичного маршрута"""

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from redis.asyncio import Redis

from app.repositories.media import MediaRepository
from app.schemas.media import MediaSummary

# здесь лежат файлы приложения, а не путь, который прислал браузер
MEDIA_DIR = Path("media")
# в очередь кладём только id записи, чтобы не хранить путь и метаданные дважды
PREVIEW_QUEUE_KEY = "media:previews"
# читаем файл кусками по 1 MiB, чтобы upload не занимал память целиком
CHUNK_SIZE = 1024 * 1024
# разные типы файлов имеют разные разумные потолки для локального прототипа
MAX_IMAGE_SIZE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_SIZE_BYTES = 200 * 1024 * 1024
MAX_AUDIO_SIZE_BYTES = 100 * 1024 * 1024
logger = logging.getLogger(__name__)


def detect_media_type(header: bytes) -> tuple[str, str] | None:
    """определить тип по сигнатуре, а не по словам клиента"""

    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm", ".webm"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        if header[8:12] in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}:
            return "video/mp4", ".mp4"
        if header[8:12] in {b"M4A ", b"M4B ", b"M4P "}:
            return "audio/mp4", ".m4a"
        if header[8:12] in {b"heic", b"heix", b"hevc", b"hevx"}:
            return "image/heic", ".heic"
    if header.startswith((b"ID3", b"\xff\xfb")):
        return "audio/mpeg", ".mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg", ".ogg"
    if header.startswith(b"fLaC"):
        return "audio/flac", ".flac"
    return None


class MediaService:
    """сохранить проверенный оригинал и поставить превью в очередь"""

    def __init__(self, repository: MediaRepository, redis_client: Redis) -> None:
        self._repository = repository
        self._redis_client = redis_client

    async def upload(self, file: UploadFile) -> MediaSummary:
        """сохранить файл только после проверки его содержимого"""

        try:
            first_chunk = await file.read(CHUNK_SIZE)
            detected = detect_media_type(first_chunk[:64])
            if detected is None:
                raise HTTPException(status_code=400, detail="file signature is not allowed")
            mime_type, extension = detected
            relative_path = Path("originals") / f"{uuid4().hex}{extension}"
            path = MEDIA_DIR / relative_path
            size = await self._save_to_disk(file, path, first_chunk, self._get_limit(mime_type))

            media = None
            try:
                media = await self._repository.create(
                    path=str(relative_path), size=size, mime_type=mime_type
                )
                try:
                    await self._redis_client.lpush(PREVIEW_QUEUE_KEY, media.id)
                except Exception:
                    # postgresql уже хранит queued задачу, worker найдёт её при следующем опросе
                    logger.warning("media %s is queued but redis wake-up failed", media.id)
            except Exception:
                # файл без записи и задания нельзя будет найти или обработать
                path.unlink(missing_ok=True)
                if media is not None:
                    await self._repository.delete(media.id)
                raise

            return MediaSummary(
                id=media.id,
                path=media.path,
                size=media.size,
                mime_type=media.mime_type,
                created_at=media.created_at,
            )
        finally:
            # закрываем временный upload и при успехе, и при любой ранней ошибке
            await file.close()

    @staticmethod
    def _get_limit(mime_type: str) -> int:
        """вернуть лимит для уже проверенного типа"""

        if mime_type.startswith("image/"):
            return MAX_IMAGE_SIZE_BYTES
        if mime_type.startswith("video/"):
            return MAX_VIDEO_SIZE_BYTES
        return MAX_AUDIO_SIZE_BYTES

    async def _save_to_disk(
        self, file: UploadFile, path: Path, first_chunk: bytes, limit: int
    ) -> int:
        """записать файл через временный путь и заменить его одним действием"""

        if len(first_chunk) > limit:
            raise HTTPException(status_code=413, detail="file is too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.uploading")
        size = len(first_chunk)
        try:
            with temporary_path.open("wb") as target:
                target.write(first_chunk)
                while chunk := await file.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(status_code=413, detail="file is too large")
                    target.write(chunk)
            temporary_path.replace(path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return size
