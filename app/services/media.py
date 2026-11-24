"""загрузка и проверка файлов медиа"""

from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from redis.asyncio import Redis

from app.repositories.media import MediaRepository
from app.schemas.media import MediaSummary

# корень хранилища оригиналов
MEDIA_DIR = Path("media")
# ключ очереди заданий для воркера превью
PREVIEW_QUEUE_KEY = "media:previews"
# размер буфера записи файла (1 МБ)
CHUNK_SIZE = 1024 * 1024

# лимиты загрузки по типам (константы, через site_settings — следующие этапы)
MAX_IMAGE_SIZE_BYTES = 50 * 1024 * 1024    # картинки: jpg, png, webp, gif, heic, heif
MAX_VIDEO_SIZE_BYTES = 200 * 1024 * 1024   # видео: mp4, webm (кружки)
MAX_AUDIO_SIZE_BYTES = 100 * 1024 * 1024   # голосовые/файлы/музыка: mp3, m4a, ogg, flac

# допустимые MIME-типы загрузки
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "image/heic", "image/heif",
    "video/mp4", "video/webm",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/flac",
}

# расширения по MIME
MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}


class MediaService:
    """загрузка, проверка и сохранение файлов медиа"""

    def __init__(self, repository: MediaRepository, redis_client: Redis) -> None:
        self._repository = repository
        self._redis_client = redis_client

    async def upload(self, file: UploadFile) -> MediaSummary:
        """проверить файл, сохранить оригинал на диск и добавить задание в очередь"""

        mime_type = self._validate_mime(file)

        # генерируем имя файла с соответствующим расширением
        path = MEDIA_DIR / "originals" / f"{uuid4().hex}{MIME_TO_EXT[mime_type]}"

        size = await self._save_to_disk(file, path, self._get_limit(mime_type))

        media = await self._repository.create(path=str(path), size=size, mime_type=mime_type)

        # запросить создание превью воркером
        await self._redis_client.lpush(PREVIEW_QUEUE_KEY, media.id)

        return MediaSummary(
            id=media.id,
            path=media.path,
            size=media.size,
            mime_type=media.mime_type,
            created_at=media.created_at,
        )

    def _get_limit(self, mime_type: str) -> int:
        """лимит загрузки для типа файла"""

        if mime_type.startswith("image/"):
            return MAX_IMAGE_SIZE_BYTES
        if mime_type.startswith("video/"):
            return MAX_VIDEO_SIZE_BYTES
        return MAX_AUDIO_SIZE_BYTES

    def _validate_mime(self, file: UploadFile) -> str:
        """проверить тип загруженного файла"""

        mime_type = file.content_type or ""
        if mime_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"file type not allowed: {mime_type or file.filename}",
            )

        return mime_type

    async def _save_to_disk(self, file: UploadFile, path: Path, limit: int) -> int:
        """записать файл на диск, проверяя размер; вернуть размер"""

        path.parent.mkdir(parents=True, exist_ok=True)

        size = 0
        with open(path, "wb") as target:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break

                size += len(chunk)
                if size > limit:
                    # удаляем частично записанный файл при ошибке
                    path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"file too large: over the limit of {limit} bytes",
                    )
                target.write(chunk)

        return size
