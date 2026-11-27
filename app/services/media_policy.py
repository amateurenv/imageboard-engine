"""единая политика вложений без транспорта и файлового хранилища"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.core.site_settings import MediaSettings
from app.schemas.boards import BoardMediaOverrides

MEBIBYTE = 1024 * 1024


class MediaType(StrEnum):
    """типы вложений, которые движок планирует поддерживать"""

    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    STICKER = "sticker"
    VIDEO_NOTE = "video_note"


class MediaPolicyCode(StrEnum):
    """стабильные коды для api, приложения и Telegram-бота"""

    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    MEDIA_FILE_TOO_LARGE = "media_file_too_large"
    MEDIA_POST_TOO_LARGE = "media_post_too_large"
    MEDIA_COUNT_EXCEEDED = "media_count_exceeded"
    MEDIA_UPLOAD_DISABLED = "media_upload_disabled"
    MEDIA_RATE_LIMITED = "media_rate_limited"
    INVALID_MEDIA = "invalid_media"


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """метаданные одного вложения до чтения или сохранения его содержимого"""

    media_type: MediaType | str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CustomEmojiEntity:
    """переносимая entity custom emoji, которая остаётся частью текста"""

    custom_emoji_id: str
    fallback_text: str


class MediaPolicyError(Exception):
    """ошибка policy с машинным кодом вместо зависимости от текста"""

    def __init__(
        self,
        *,
        code: MediaPolicyCode,
        message: str,
        http_status: int,
        limit_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.limit_bytes = limit_bytes

    def to_detail(self) -> dict[str, str | int]:
        """вернуть будущий JSON detail без привязки к FastAPI"""

        detail: dict[str, str | int] = {"code": self.code.value, "message": self.message}
        if self.limit_bytes is not None:
            detail["limit_bytes"] = self.limit_bytes
        return detail


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    """эффективные байтовые лимиты одной доски"""

    max_attachments_per_post: int
    max_attachment_size_bytes: int
    max_total_size_bytes: int
    uploads_enabled: bool
    allow_anonymous_uploads: bool

    @classmethod
    def from_settings(
        cls,
        settings: MediaSettings,
        overrides: BoardMediaOverrides | None = None,
    ) -> MediaPolicy:
        """слить глобальную политику и только заданные значения доски"""

        board = overrides or BoardMediaOverrides()
        return cls(
            max_attachments_per_post=(
                board.max_attachments_per_post or settings.max_attachments_per_post
            ),
            max_attachment_size_bytes=(
                board.max_attachment_size_mib or settings.max_attachment_size_mib
            )
            * MEBIBYTE,
            max_total_size_bytes=(
                board.max_total_size_mib or settings.max_total_size_mib
            )
            * MEBIBYTE,
            uploads_enabled=(
                settings.uploads_enabled
                if board.uploads_enabled is None
                else board.uploads_enabled
            ),
            allow_anonymous_uploads=(
                settings.allow_anonymous_uploads
                if board.allow_anonymous_uploads is None
                else board.allow_anonymous_uploads
            ),
        )

    def validate(
        self,
        attachments: Iterable[MediaAttachment],
        *,
        custom_emoji_entities: Iterable[CustomEmojiEntity] = (),
    ) -> None:
        """проверить метаданные вложений без доверия mime type или имени файла"""

        # entity остаются в тексте и сознательно не влияют на число или размер вложений
        tuple(custom_emoji_entities)
        items = tuple(attachments)
        if not self.uploads_enabled:
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_UPLOAD_DISABLED,
                message="загрузка вложений отключена для этой доски",
                http_status=403,
            )
        if len(items) > self.max_attachments_per_post:
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_COUNT_EXCEEDED,
                message=(
                    "в посте больше вложений, чем разрешено для доски: "
                    f"{self.max_attachments_per_post}"
                ),
                http_status=422,
            )

        total_size = 0
        for attachment in items:
            media_type = self._parse_media_type(attachment.media_type)
            if attachment.size_bytes < 0:
                raise self.invalid_media("размер вложения не может быть отрицательным")
            if attachment.size_bytes > self.max_attachment_size_bytes:
                limit_mib = self.max_attachment_size_bytes // MEBIBYTE
                raise MediaPolicyError(
                    code=MediaPolicyCode.MEDIA_FILE_TOO_LARGE,
                    message=(
                        f"размер {media_type.value} больше лимита доски: {limit_mib} MiB"
                    ),
                    http_status=413,
                    limit_bytes=self.max_attachment_size_bytes,
                )
            total_size += attachment.size_bytes

        if total_size > self.max_total_size_bytes:
            limit_mib = self.max_total_size_bytes // MEBIBYTE
            raise MediaPolicyError(
                code=MediaPolicyCode.MEDIA_POST_TOO_LARGE,
                message=f"общий размер вложений больше лимита доски: {limit_mib} MiB",
                http_status=413,
                limit_bytes=self.max_total_size_bytes,
            )

    @staticmethod
    def rate_limited(message: str = "слишком много загрузок, попробуйте позже") -> MediaPolicyError:
        """дать антиспаму единый ответ до появления конкретного транспорта"""

        return MediaPolicyError(
            code=MediaPolicyCode.MEDIA_RATE_LIMITED,
            message=message,
            http_status=429,
        )

    @staticmethod
    def invalid_media(
        message: str = "файл повреждён или не соответствует заявленному типу",
    ) -> MediaPolicyError:
        """дать будущей проверке содержимого единый ответ"""

        return MediaPolicyError(
            code=MediaPolicyCode.INVALID_MEDIA,
            message=message,
            http_status=422,
        )

    @staticmethod
    def _parse_media_type(media_type: MediaType | str) -> MediaType:
        """принять только объявленные типы до обращения к файловому обработчику"""

        try:
            return MediaType(media_type)
        except ValueError as error:
            raise MediaPolicyError(
                code=MediaPolicyCode.UNSUPPORTED_MEDIA_TYPE,
                message="этот тип вложения не поддерживается",
                http_status=415,
            ) from error
