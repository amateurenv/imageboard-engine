"""первый безопасный multipart upload только для изображений"""

import asyncio
import logging
from pathlib import Path

from asyncpg import Connection, Pool
from fastapi import UploadFile
from redis.asyncio import Redis

from app.core.security import hash_token
from app.core.site_settings import SiteSettings
from app.repositories.media import MediaRecord, MediaRepository
from app.repositories.posting_claims import ClaimBoard, PostingClaim, PostingClaimsRepository
from app.repositories.users import UserRecord
from app.services.image_validation import validate_image
from app.services.media_policy import (
    MediaAttachment,
    MediaPolicy,
    MediaPolicyCode,
    MediaPolicyError,
    MediaType,
)
from app.services.media_storage import MediaStorage, MediaStorageError
from app.services.posting_claims import PostingClaimError
from app.services.posting_policy import (
    PostingBoard,
    PostingPermissionError,
    check_posting_permission,
)

# потоковая запись ограничивает память одного запроса даже для заявленного большого файла
UPLOAD_CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class MediaUploadService:
    """сохранить image в quarantine и создать только queued запись media"""

    def __init__(
        self,
        pool: Pool,
        redis_client: Redis,
        settings: SiteSettings,
        storage_root: Path,
    ) -> None:
        self._pool = pool
        self._redis = redis_client
        self._settings = settings
        self._storage = MediaStorage(storage_root)
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
        """принять изображение без доверия имени, MIME или размеру клиента"""

        path: str | None = None
        try:
            board = await self._claims.get_board(board_slug)
            if board is None:
                raise PostingClaimError("board_not_found", 404)
            self._check_any_posting_policy(board, actor)
            policy = self._policy(board)
            self._ensure_upload_enabled(policy, actor)
            self._check_declared_size(content_length, policy)

            # имя не связано с filename клиента и никогда не возвращается api
            path = self._storage.new_quarantine_path(".image")
            size = await self._stream_to_quarantine(file, path, policy)
            image = await asyncio.to_thread(validate_image, self._storage.resolve(path))

            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    locked_board = await self._claims.lock_board(connection, board_slug)
                    if locked_board is None:
                        raise PostingClaimError("board_not_found", 404)
                    self._check_any_posting_policy(locked_board, actor)
                    current_policy = self._policy(locked_board)
                    self._ensure_upload_enabled(current_policy, actor)
                    claim = await self._lock_valid_claim(
                        connection, token, locked_board, actor
                    )
                    existing_sizes = await self._claims.lock_unattached_media_sizes(
                        connection, claim.id
                    )
                    # policy может стать строже после выдачи claim, поэтому проверяем её снова
                    attachments = [
                        MediaAttachment(MediaType.PHOTO, item) for item in existing_sizes
                    ]
                    attachments.append(MediaAttachment(image.media_type, size))
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
                        mime_type=image.mime_type,
                        board_id=locked_board.id,
                        posting_claim_id=claim.id,
                    )
            try:
                # postgres хранит queued источник истины, redis только сокращает ожидание worker
                await self._redis.lpush("media:previews", media.id)
            except Exception:
                logger.warning("media %s is queued but redis wake-up failed", media.id)
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
                            message="размер изображения больше лимита доски",
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
        if declared_size > policy.max_attachment_size_bytes:
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
                message="заявленный размер upload больше лимита доски",
                http_status=413,
                limit_bytes=policy.max_attachment_size_bytes,
            )

    async def _lock_valid_claim(
        self,
        connection: Connection,
        token: str,
        board: ClaimBoard,
        actor: UserRecord | None,
    ) -> PostingClaim:
        """проверить одноразовый claim под блокировкой перед созданием media"""

        claim = await self._claims.lock_claim(connection, hash_token(token))
        from datetime import UTC, datetime

        if claim is None or claim.consumed_at is not None or claim.expires_at <= datetime.now(UTC):
            raise PostingClaimError("posting_claim_invalid", 403)
        actor_id = None if actor is None else actor.id
        if claim.board_id != board.id or claim.account_user_id != actor_id:
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
