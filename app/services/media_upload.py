"""multipart upload проверенных вложений в закрытый quarantine"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asyncpg import Pool
from fastapi import UploadFile
from redis.asyncio import Redis

from app.core.security import hash_token
from app.core.site_settings import SiteSettings
from app.repositories.media import MediaRecord, MediaRepository
from app.repositories.posting_claims import ClaimBoard, PostingClaim, PostingClaimsRepository
from app.repositories.users import UserRecord
from app.services.audio_processing import AudioInspection, FFmpegMediaProcessor
from app.services.image_validation import validate_image
from app.services.media_policy import (
    MediaAttachment,
    MediaPolicy,
    MediaPolicyCode,
    MediaPolicyError,
    MediaType,
)
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.posting_claims import (
    PostingClaimError,
    StickerCannotStartThread,
    sticker_reply_policy,
)
from app.services.posting_policy import (
    PostingBoard,
    PostingPermissionError,
    check_posting_permission,
)
from app.services.sticker_processing import STICKER_MIME, decode_sticker
from app.services.video_processing import VIDEO_MIME, VideoProcessor, looks_like_mp4_video

# потоковая запись ограничивает память одного запроса даже для заявленного большого файла
UPLOAD_CHUNK_SIZE = 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadInspection:
    """общий безопасный результат image decoder или ffprobe без имени клиента"""

    mime_type: str
    media_type: MediaType
    is_animation: bool = False
    duration_seconds: int | None = None
    channels: int | None = None
    sample_rate: int | None = None


class MediaUploadService:
    """сохранить файл в quarantine и создать только queued запись media"""

    def __init__(
        self,
        pool: Pool,
        redis_client: Redis,
        settings: SiteSettings,
        storage_root: Path,
        audio_processor: FFmpegMediaProcessor | None = None,
        video_processor: VideoProcessor | None = None,
    ) -> None:
        self._pool = pool
        self._redis = redis_client
        self._settings = settings
        self._storage = MediaStorage(storage_root)
        self._audio_processor = audio_processor or FFmpegMediaProcessor()
        self._video_processor = video_processor or VideoProcessor()
        self._claims = PostingClaimsRepository(pool)
        self._media = MediaRepository(pool)

    async def upload(
        self,
        *,
        board_slug: str,
        token: str,
        file: UploadFile,
        actor: UserRecord | None,
        content_length: str | None,
    ) -> MediaRecord:
        """принять вложение без доверия имени, MIME или размеру клиента"""

        path: str | None = None
        try:
            board = await self._claims.get_board(board_slug)
            if board is None:
                raise PostingClaimError("board_not_found", 404)
            self._check_any_posting_policy(board, actor)
            policy = self._policy(board)
            self._ensure_upload_enabled(policy, actor)
            self._check_declared_size(content_length, policy)
            token_hash = hash_token(token)
            self._check_claim(await self._claims.get_claim(token_hash), board, actor)

            # имя не связано с filename клиента и никогда не возвращается api
            path = self._storage.new_quarantine_path(".upload")
            size = await self._stream_to_quarantine(file, path, policy)
            inspected = await asyncio.to_thread(self._inspect, self._storage.resolve(path))

            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    locked_board = await self._claims.lock_board(connection, board_slug)
                    if locked_board is None:
                        raise PostingClaimError("board_not_found", 404)
                    self._check_any_posting_policy(locked_board, actor)
                    current_policy = self._policy(locked_board)
                    self._ensure_upload_enabled(current_policy, actor)
                    claim = self._check_claim(
                        await self._claims.lock_claim(connection, token_hash), locked_board, actor
                    )
                    existing_sizes = await self._claims.lock_unattached_media_sizes(
                        connection, claim.id
                    )
                    # policy может стать строже после выдачи claim, поэтому проверяем её снова
                    attachments = [
                        MediaAttachment(MediaType.PHOTO, item) for item in existing_sizes
                    ]
                    attachments.append(MediaAttachment(inspected.media_type, size))
                    current_policy.validate(attachments)
                    if len(attachments) > claim.max_attachments:
                        raise MediaPolicyError(
                            code=MediaPolicyCode.MEDIA_COUNT_EXCEEDED,
                            message="в posting claim больше вложений, чем разрешено",
                            http_status=422,
                        )
                    media = await self._media.create_in_transaction(
                        connection,
                        path=path,
                        size=size,
                        mime_type=inspected.mime_type,
                        is_animation=inspected.is_animation,
                        board_id=locked_board.id,
                        posting_claim_id=claim.id,
                        media_kind=(
                            "image"
                            if inspected.media_type in {MediaType.PHOTO, MediaType.ANIMATION}
                            else inspected.media_type.value
                        ),
                        duration_seconds=inspected.duration_seconds,
                        channels=inspected.channels,
                        sample_rate=inspected.sample_rate,
                    )
            await self._wake_worker(media.id)
            return media
        except Exception:
            if path is not None:
                try:
                    self._storage.delete_file(path)
                except (MediaStorageError, OSError):
                    logger.exception("could not remove failed quarantine upload")
            raise
        finally:
            await file.close()

    def _inspect(self, path: Path) -> UploadInspection:
        """выбрать decoder по содержимому и назначить публичному MP4 обычный тип video"""

        if looks_like_mp4_video(path):
            video = self._video_processor.inspect(path, MediaType.VIDEO)
            return UploadInspection(
                VIDEO_MIME, MediaType.VIDEO,
                duration_seconds=video.duration_seconds,
                channels=video.channels,
                sample_rate=video.sample_rate,
            )
        try:
            image = validate_image(path)
        except MediaPolicyError as image_error:
            if not self._audio_processor.looks_like_audio_candidate(path):
                raise image_error
            audio = self._audio_processor.inspect(path)
            return self._audio_inspection(audio)
        return UploadInspection(image.mime_type, image.media_type, image.is_animation)

    async def stage_sticker(
        self, *, thread_id: int | None, actor: UserRecord, data: bytes,
    ) -> tuple[str, MediaRecord]:
        """поставить один стикер в очередь от доверенного адаптера с аккаунтом"""

        if thread_id is None:
            raise StickerCannotStartThread()
        image = await asyncio.to_thread(decode_sticker, data)
        image.close()
        token = secrets.token_urlsafe(32)
        staging = asyncio.create_task(self._store_sticker(thread_id, actor, token, data))
        try:
            media = await asyncio.shield(staging)
        except asyncio.CancelledError:
            # дождаться commit или отката - закрытый claim истечёт, если адаптер ушёл
            await asyncio.gather(staging, return_exceptions=True)
            raise
        await self._wake_worker(media.id)
        return token, media

    async def _store_sticker(
        self, thread_id: int, actor: UserRecord, token: str, data: bytes,
    ) -> MediaRecord:
        """сохранить закрытый claim и задачу обработки с общим commit"""

        path: str | None = None
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    board = await self._claims.lock_sticker_target(connection, thread_id)
                    if board is None:
                        raise PostingClaimError("sticker_thread_unavailable", 404)
                    sticker_reply_policy(board, actor, self._settings).validate(
                        [MediaAttachment(MediaType.STICKER, len(data))]
                    )
                    claim_id = await self._claims.create_sticker_claim(
                        connection, board.id, thread_id, actor.id, hash_token(token),
                        timedelta(minutes=self._settings.posting_claims.ttl_minutes),
                    )
                    relative_path = self._storage.new_quarantine_path(".upload")
                    descriptor, _ = self._storage.open_new(relative_path)
                    path = relative_path
                    with open(descriptor, "wb", closefd=True) as target:
                        target.write(data)
                    media = await self._media.create_in_transaction(
                        connection, path=path, size=len(data), mime_type=STICKER_MIME,
                        board_id=board.id, posting_claim_id=claim_id,
                        is_animation=False, media_kind=MediaType.STICKER.value,
                    )
        except Exception:
            if path is not None:
                try:
                    self._storage.delete_file(path)
                except (MediaStorageError, OSError):
                    logger.exception("could not remove failed quarantine sticker")
            raise
        return media

    async def _wake_worker(self, media_id: int) -> None:
        """сократить ожидание worker, сохраняя очередь в postgres"""

        try:
            await self._redis.lpush("media:previews", media_id)
        except Exception:
            logger.warning("media %s is queued but redis wake-up failed", media_id)

    @staticmethod
    def _audio_inspection(audio: AudioInspection) -> UploadInspection:
        """не переносить из ffprobe ничего кроме разрешённых технических чисел"""

        return UploadInspection(
            audio.source_mime_type,
            audio.media_type,
            duration_seconds=audio.duration_seconds,
            channels=audio.channels,
            sample_rate=audio.sample_rate,
        )

    async def _stream_to_quarantine(
        self, file: UploadFile, relative_path: str, policy: MediaPolicy
    ) -> int:
        """записать upload кусками и остановиться сразу после фактического лимита"""

        descriptor, path = self._storage.open_new(relative_path)
        size = 0
        try:
            with open(descriptor, "wb", closefd=True) as target:
                while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > policy.max_attachment_size_bytes:
                        raise MediaPolicyError(
                            code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
                            message="размер вложения больше лимита доски",
                            http_status=413,
                            limit_bytes=policy.max_attachment_size_bytes,
                        )
                    target.write(chunk)
            return size
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _check_declared_size(content_length: str | None, policy: MediaPolicy) -> None:
        """раньше отклонить честный oversized multipart body без доверия этому заголовку"""

        if content_length is None:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > policy.max_attachment_size_bytes + MULTIPART_OVERHEAD_BYTES:
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
                message="заявленный размер upload больше лимита доски",
                http_status=413,
                limit_bytes=policy.max_attachment_size_bytes,
            )

    @staticmethod
    def _check_claim(
        claim: PostingClaim | None,
        board: ClaimBoard,
        actor: UserRecord | None,
    ) -> PostingClaim:
        """проверить claim до чтения файла и повторно под блокировкой перед записью"""

        if claim is None or claim.consumed_at is not None or claim.expires_at <= datetime.now(UTC):
            raise PostingClaimError("posting_claim_invalid", 403)
        actor_id = None if actor is None else actor.id
        if claim.board_id != board.id or claim.account_user_id != actor_id:
            raise PostingClaimError("posting_claim_forbidden", 403)
        if claim.sticker_thread_id is not None:
            raise PostingClaimError("posting_claim_forbidden", 403)
        return claim

    def _policy(self, board: ClaimBoard) -> MediaPolicy:
        """собрать текущие глобальные и board-level пределы"""

        from app.schemas.boards import BoardMediaOverrides

        return MediaPolicy.from_settings(
            self._settings.media, BoardMediaOverrides.model_validate(board.media_overrides)
        )

    @staticmethod
    def _ensure_upload_enabled(policy: MediaPolicy, actor: UserRecord | None) -> None:
        """не позволить anonymous upload при закрытой доске"""

        if not policy.uploads_enabled or (actor is None and not policy.allow_anonymous_uploads):
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_UPLOAD_DISABLED,
                message="загрузка вложений отключена для доски",
                http_status=403,
            )

    @staticmethod
    def _check_any_posting_policy(board: ClaimBoard, actor: UserRecord | None) -> None:
        """claim допустим для треда или ответа, финальная публикация проверяется отдельно"""

        posting_board = PostingBoard(
            board.board_type, board.owner_user_id,
            board.thread_creation_policy, board.reply_policy,
        )
        try:
            check_posting_permission(
                posting_board, policy=board.thread_creation_policy,
                actor_user_id=None if actor is None else actor.id,
                actor_role=None if actor is None else actor.role,
            )
        except PostingPermissionError as thread_error:
            try:
                check_posting_permission(
                    posting_board, policy=board.reply_policy,
                    actor_user_id=None if actor is None else actor.id,
                    actor_role=None if actor is None else actor.role,
                )
            except PostingPermissionError as reply_error:
                raise thread_error from reply_error
