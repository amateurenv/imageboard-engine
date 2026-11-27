"""авторизация готового derivative media перед internal nginx redirect"""

import re
from dataclasses import dataclass
from typing import Protocol

from app.repositories.media_delivery import DeliverableMedia, MediaDeliveryRepository
from app.repositories.users import UserRecord
from app.services.video_processing import VIDEO_KINDS, VIDEO_MIME

INTERNAL_ACCEL_PREFIX = "/_imageboard_media"
PUBLIC_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class MediaDeliveryAccess(Protocol):
    """точка расширения для будущих private profile boards без фиктивной auth модели"""

    def allows(self, media: DeliverableMedia, actor: UserRecord | None) -> bool:
        """разрешить или запретить выдачу уже видимого media"""


class CurrentPublicMediaAccess:
    """все существующие active public и profile boards читаются анонимно"""

    def allows(self, media: DeliverableMedia, actor: UserRecord | None) -> bool:
        """оставить actor в контракте для будущей private-доски"""

        del actor
        return media.board_type in {"public", "profile"}


@dataclass(frozen=True, slots=True)
class MediaDelivery:
    """готовый внутренний redirect после повторной проверки видимости в базе"""

    content_type: str
    accel_path: str
    cache_control: str = "public, max-age=31536000, immutable"


class MediaDeliveryService:
    """выдать только nginx-internal путь производного файла"""

    def __init__(
        self,
        repository: MediaDeliveryRepository,
        access: MediaDeliveryAccess | None = None,
    ) -> None:
        self._repository = repository
        self._access = access or CurrentPublicMediaAccess()

    async def resolve(
        self,
        public_key: str,
        asset_kind: str,
        actor: UserRecord | None,
    ) -> MediaDelivery | None:
        """проверить key, состояние, attach и видимость перед каждым redirect"""

        if PUBLIC_KEY_PATTERN.fullmatch(public_key) is None:
            return None
        if asset_kind not in {"preview", "display", "content"}:
            return None
        media = await self._repository.get_deliverable(public_key)
        if media is None or not self._access.allows(media, actor):
            return None
        if media.media_kind in VIDEO_KINDS:
            video_path = f"video/{media.id}.mp4"
            preview_name = f"{media.id}.webp"
            if (
                media.canonical_path != video_path
                or media.canonical_mime_type != VIDEO_MIME
                or media.preview_path != f"previews/{preview_name}"
            ):
                return None
            # у видео и его preview одинаковые правила кеша после модерации
            if asset_kind == "content":
                return MediaDelivery(
                    VIDEO_MIME, f"{INTERNAL_ACCEL_PREFIX}/{video_path}", cache_control="no-store",
                )
            if asset_kind == "preview":
                return MediaDelivery(
                    "image/webp",
                    f"{INTERNAL_ACCEL_PREFIX}/video-previews/{preview_name}",
                    cache_control="no-store",
                )
            return None
        if asset_kind == "content":
            if media.media_kind not in {"audio", "voice"} or media.canonical_path is None:
                return None
            extension = "m4a" if media.media_kind == "audio" else "ogg"
            if media.canonical_path != f"{media.media_kind}/{media.id}.{extension}":
                return None
            return MediaDelivery(
                content_type=media.canonical_mime_type or "application/octet-stream",
                accel_path=f"{INTERNAL_ACCEL_PREFIX}/{media.canonical_path}",
            )
        if media.media_kind != "image":
            return None
        relative_path = media.preview_path if asset_kind == "preview" else media.display_path
        directory = "previews" if asset_kind == "preview" else "display"
        if relative_path != f"{directory}/{media.id}.webp":
            # значение базы не становится заголовком, если не совпало с нашим форматом worker
            return None
        return MediaDelivery(
            content_type="image/webp",
            accel_path=f"{INTERNAL_ACCEL_PREFIX}/{relative_path}",
        )
