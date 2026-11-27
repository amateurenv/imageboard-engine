"""регистрация и правила изменения личных досок"""

import re
from typing import cast

from asyncpg import UniqueViolationError

from app.core.site_settings import SiteSettings
from app.core.urls import board_canonical_url
from app.repositories.profiles import ProfileBoardRecord, ProfilesRepository
from app.repositories.users import UserRecord, UsersRepository
from app.schemas.profiles import PostingPolicy, ProfileBoard, ProfileBoardUpdate
from app.schemas.users import AccountPublic


class RegistrationError(ValueError):
    """handle нельзя зарегистрировать по правилам сайта"""


class ProfileAccessError(PermissionError):
    """текущий пользователь не владеет profile-доской"""


class ProfilesService:
    """общая identity-модель обычных пользователей и staff"""

    def __init__(
        self,
        users_repository: UsersRepository,
        profiles_repository: ProfilesRepository,
        site_settings: SiteSettings,
    ) -> None:
        self._users_repository = users_repository
        self._profiles_repository = profiles_repository
        self._site_settings = site_settings

    async def register(self, handle: str, password_hash: str) -> AccountPublic:
        """создать обычный аккаунт и его profile-доску одной транзакцией"""

        normalized_handle = self._validate_handle(handle)
        try:
            user = await self._users_repository.create_profile_user(
                normalized_handle, password_hash
            )
        except UniqueViolationError as error:
            raise RegistrationError("handle is already taken") from error
        return self._to_account(user)

    async def get_profile(self, handle: str) -> ProfileBoard | None:
        """вернуть публичные поля profile-доски"""

        profile = await self._profiles_repository.get_by_handle(handle.lower())
        return None if profile is None else self._to_profile(profile)

    async def update_profile(
        self,
        *,
        handle: str,
        actor: UserRecord,
        update: ProfileBoardUpdate,
    ) -> ProfileBoard | None:
        """разрешить владельцу менять только разрешённые поля"""

        current = await self._profiles_repository.get_by_handle(handle.lower())
        if current is None:
            return None
        if actor.id != current.owner_user_id and actor.role not in {"owner", "admin", "moderator"}:
            raise ProfileAccessError("profile board belongs to another user")

        values = update.model_dump(exclude_none=True)
        updated = await self._profiles_repository.update_owned(
            handle=current.handle,
            owner_user_id=current.owner_user_id,
            name=values.get("name", current.name),
            description=values.get("description", current.description),
            rules=values.get("rules", current.rules),
            thread_creation_policy=values.get(
                "thread_creation_policy", current.thread_creation_policy
            ),
            reply_policy=values.get("reply_policy", current.reply_policy),
        )
        return None if updated is None else self._to_profile(updated)

    def _validate_handle(self, handle: str) -> str:
        """проверить новые handle без ретроактивной проверки старых аккаунтов"""

        normalized = handle.lower()
        limits = self._site_settings.identifiers
        if not (
            limits.profile_handle_min_length
            <= len(normalized)
            <= limits.profile_handle_max_length
        ):
            raise RegistrationError("handle length is outside site limits")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", normalized) is None:
            raise RegistrationError(
                "handle may contain only lowercase ASCII letters, digits, _ and -"
            )
        return normalized

    @staticmethod
    def _to_account(user: UserRecord) -> AccountPublic:
        """не раскрыть пароль при ответе регистрации"""

        return AccountPublic(
            id=user.id,
            handle=user.login,
            role=user.role,
            created_at=user.created_at,
            profile_url=board_canonical_url("profile", user.login),
        )

    @staticmethod
    def _to_profile(profile: ProfileBoardRecord) -> ProfileBoard:
        """собрать транспортный ответ с canonical url"""

        return ProfileBoard(
            handle=profile.handle,
            name=profile.name,
            description=profile.description,
            rules=profile.rules,
            thread_creation_policy=cast(PostingPolicy, profile.thread_creation_policy),
            reply_policy=cast(PostingPolicy, profile.reply_policy),
            canonical_url=board_canonical_url("profile", profile.handle),
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )
