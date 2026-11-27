"""claims для будущего поста без public upload транспорта"""

# ruff: noqa: E501 - транзакционные вызовы с полными аргументами читаются целиком

import secrets
from datetime import UTC, datetime, timedelta

from asyncpg import Connection, Pool

from app.core.security import hash_token
from app.core.site_settings import SiteSettings
from app.repositories.posting_claims import ClaimBoard, PostingClaim, PostingClaimsRepository
from app.repositories.threads import PostCreationResult, ThreadCreationResult, ThreadsRepository
from app.repositories.users import UserRecord
from app.schemas.boards import BoardMediaOverrides
from app.services.media_policy import (
    MediaAttachment,
    MediaPolicy,
    MediaPolicyCode,
    MediaPolicyError,
    MediaType,
)
from app.services.posting_policy import (
    PostingBoard,
    PostingPermissionError,
    check_posting_permission,
)


class PostingClaimError(ValueError):
    """стабильная ошибка claim без раскрытия token или чужого media"""

    def __init__(self, code: str, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class PostingClaimsService:
    """создать и одноразово употребить право подготовить media"""

    def __init__(self, pool: Pool, settings: SiteSettings) -> None:
        self._pool = pool
        self._settings = settings
        self._claims = PostingClaimsRepository(pool)
        self._threads = ThreadsRepository(pool)

    async def create(self, slug: str, actor: UserRecord | None) -> tuple[str, PostingClaim]:
        """выдать token один раз после проверки policy новой публикации"""

        board = await self._claims.get_board(slug)
        if board is None:
            raise PostingClaimError("board_not_found", 404)
        # claim может понадобиться и для треда, и для ответа - финальный путь
        # всё равно повторно проверит конкретное правило перед созданием
        try:
            self._check_policy(board, board.thread_creation_policy, actor)
        except PostingPermissionError as thread_error:
            try:
                self._check_policy(board, board.reply_policy, actor)
            except PostingPermissionError as reply_error:
                raise thread_error from reply_error
        policy = self._policy(board)
        if not policy.uploads_enabled or (actor is None and not policy.allow_anonymous_uploads):
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_UPLOAD_DISABLED,
                message="загрузка вложений отключена для доски",
                http_status=403,
            )
        token = secrets.token_urlsafe(32)
        claim = await self._claims.create(
            board.id,
            None if actor is None else actor.id,
            hash_token(token),
            timedelta(minutes=self._settings.posting_claims.ttl_minutes),
            policy.max_attachments_per_post,
        )
        return token, claim

    async def create_thread_with_media(
        self, board: ClaimBoard, actor: UserRecord | None, subject: str, author_name: str | None,
        body: str, deletion_hash: str | None, token: str, media_ids: list[int],
    ) -> ThreadCreationResult:
        """создать тред и прикрепить media без промежуточного коммита"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                claim = await self._validate_claim(connection, board, actor, token, media_ids)
                result = await self._threads.create_thread_in_transaction(
                    connection, board_id=board.id, subject=subject, author_name=author_name,
                    body=body, author_user_id=None if actor is None else actor.id,
                    deletion_password_hash=deletion_hash,
                )
                await self._claims.attach_and_consume(connection, claim.id, result.post.id, media_ids)
        return result

    async def create_post_with_media(
        self, board: ClaimBoard, thread_id: int, actor: UserRecord | None, author_name: str | None,
        body: str, references: list[int], deletion_hash: str | None, token: str,
        media_ids: list[int],
    ) -> PostCreationResult:
        """создать ответ и прикрепить media без частичного состояния"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                claim = await self._validate_claim(connection, board, actor, token, media_ids)
                result = await self._threads.create_post_in_transaction(
                    connection, thread_id=thread_id, author_name=author_name, body=body,
                    reference_ids=references, author_user_id=None if actor is None else actor.id,
                    deletion_password_hash=deletion_hash,
                )
                await self._claims.attach_and_consume(connection, claim.id, result.post.id, media_ids)
        return result

    async def _validate_claim(self, connection: Connection, board: ClaimBoard, actor: UserRecord | None,
                              token: str, media_ids: list[int]) -> PostingClaim:
        if not token or not media_ids or len(set(media_ids)) != len(media_ids):
            raise PostingClaimError("posting_claim_required")
        claim = await self._claims.lock_claim(connection, hash_token(token))
        if claim is None or claim.consumed_at is not None or claim.expires_at <= datetime.now(UTC):
            raise PostingClaimError("posting_claim_invalid", 403)
        if claim.board_id != board.id or claim.account_user_id != (None if actor is None else actor.id):
            raise PostingClaimError("posting_claim_forbidden", 403)
        if len(media_ids) > claim.max_attachments:
            raise PostingClaimError("media_count_exceeded")
        media = await self._claims.lock_media(connection, claim.id, board.id, media_ids)
        if len(media) != len(media_ids):
            raise PostingClaimError("posting_claim_media_invalid", 403)
        self._policy(board).validate([MediaAttachment(self._media_type(item.mime_type), item.size) for item in media])
        return claim

    def _policy(self, board: ClaimBoard) -> MediaPolicy:
        return MediaPolicy.from_settings(
            self._settings.media, BoardMediaOverrides.model_validate(board.media_overrides)
        )

    @staticmethod
    def _media_type(mime_type: str) -> MediaType:
        if mime_type.startswith("image/gif"):
            return MediaType.ANIMATION
        if mime_type.startswith("image/"):
            return MediaType.PHOTO
        if mime_type.startswith("video/"):
            return MediaType.VIDEO
        raise PostingClaimError("unsupported_media_type", 415)

    @staticmethod
    def _check_policy(board: ClaimBoard, policy: str, actor: UserRecord | None) -> None:
        check_posting_permission(
            PostingBoard(board.board_type, board.owner_user_id, board.thread_creation_policy, board.reply_policy),
            policy=policy, actor_user_id=None if actor is None else actor.id,
            actor_role=None if actor is None else actor.role,
        )
