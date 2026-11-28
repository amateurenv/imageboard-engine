"""зависимости для HTTP-маршрутов"""

from hmac import compare_digest
from ipaddress import ip_address
from typing import Annotated

from asyncpg import Pool
from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from starlette.requests import HTTPConnection

from app.core.config import get_settings
from app.core.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, hash_token
from app.core.site_settings import SiteSettings
from app.repositories.ip_bans import IpBansRepository
from app.repositories.media_delivery import MediaDeliveryRepository
from app.repositories.profiles import ProfilesRepository
from app.repositories.threads import ThreadsRepository
from app.repositories.users import UserRecord, UsersRepository
from app.services.audio_processing import FFmpegMediaProcessor
from app.services.media_delivery import MediaDeliveryService
from app.services.media_upload import MediaUploadService
from app.services.posting_claims import PostingClaimsService
from app.services.profile_moderation import ProfileModerationService
from app.services.profiles import ProfilesService
from app.services.self_delete import SelfDeleteService
from app.services.spam import SpamService
from app.services.staff import StaffService
from app.services.threads import ThreadsService
from app.services.users import UsersService
from app.services.video_processing import VideoProcessor
from app.ws.manager import WebSocketManager


def get_postgres_pool(request: HTTPConnection) -> Pool:
    """вернуть общий пул подключений к postgresql"""

    # пул создаётся при запуске fastapi и хранится в состоянии приложения
    return request.app.state.postgres_pool


def get_redis_client(request: Request) -> Redis:
    """вернуть общий клиент redis"""

    # клиент создаётся при запуске fastapi и хранится в состоянии приложения
    return request.app.state.redis_client


def get_client_ip(request: Request) -> str:
    """вернуть IP клиента, доверяя proxy только из списка настроек"""

    peer_ip = request.client.host if request.client is not None else ""
    trusted_proxy_ips = {
        item.strip()
        for item in get_settings().trusted_proxy_ips.split(",")
        if item.strip()
    }

    if peer_ip not in trusted_proxy_ips:
        return peer_ip

    forwarded_for = request.headers.get("x-forwarded-for", "")
    candidate_ip = forwarded_for.split(",", maxsplit=1)[0].strip()
    if not candidate_ip:
        return peer_ip

    try:
        return str(ip_address(candidate_ip))
    except ValueError:
        return peer_ip


def get_site_settings(request: Request) -> SiteSettings:
    """вернуть проверенные настройки сайта"""

    # настройки загружаются из postgresql один раз при запуске
    return request.app.state.site_settings


# эти короткие имена будут использоваться в параметрах маршрутов
type PostgresPoolDependency = Annotated[
    Pool,
    Depends(get_postgres_pool),
]

type RedisClientDependency = Annotated[
    Redis,
    Depends(get_redis_client),
]

type SiteSettingsDependency = Annotated[
    SiteSettings,
    Depends(get_site_settings),
]


def get_ws_manager(connection: HTTPConnection) -> WebSocketManager:
    """вернуть websocket-менеджер приложения"""

    return connection.app.state.ws_manager


type WsManagerDependency = Annotated[
    WebSocketManager,
    Depends(get_ws_manager),
]


def get_threads_service(
    postgres_pool: PostgresPoolDependency,
    site_settings: SiteSettingsDependency,
) -> ThreadsService:
    """создать service для одного http-запроса"""

    # эти объекты лёгкие и не создают новое подключение к postgresql
    repository = ThreadsRepository(postgres_pool)

    claims = PostingClaimsService(postgres_pool, site_settings)
    return ThreadsService(repository, site_settings, claims)


type ThreadsServiceDependency = Annotated[
    ThreadsService,
    Depends(get_threads_service),
]


def get_posting_claims_service(
    postgres_pool: PostgresPoolDependency, site_settings: SiteSettingsDependency
) -> PostingClaimsService:
    """создать service одноразовых прав подготовки медиа"""

    return PostingClaimsService(postgres_pool, site_settings)


type PostingClaimsServiceDependency = Annotated[
    PostingClaimsService,
    Depends(get_posting_claims_service),
]


def get_users_repository(postgres_pool: PostgresPoolDependency) -> UsersRepository:
    """создать репозиторий сотрудников"""

    return UsersRepository(postgres_pool)


def get_ip_bans_repository(postgres_pool: PostgresPoolDependency) -> IpBansRepository:
    """создать репозиторий IP-банов"""

    return IpBansRepository(postgres_pool)


def get_users_service(
    postgres_pool: PostgresPoolDependency,
    redis_client: RedisClientDependency,
) -> UsersService:
    """создать сервис авторизации"""

    return UsersService(UsersRepository(postgres_pool), redis_client)


type UsersServiceDependency = Annotated[
    UsersService,
    Depends(get_users_service),
]


def get_profiles_service(
    postgres_pool: PostgresPoolDependency,
    site_settings: SiteSettingsDependency,
) -> ProfilesService:
    """создать сервис регистрации и profile-досок"""

    return ProfilesService(
        UsersRepository(postgres_pool),
        ProfilesRepository(postgres_pool),
        site_settings,
    )


type ProfilesServiceDependency = Annotated[
    ProfilesService,
    Depends(get_profiles_service),
]


def get_profile_moderation_service(
    postgres_pool: PostgresPoolDependency,
) -> ProfileModerationService:
    """создать service owner-модерации без отдельной auth модели"""

    return ProfileModerationService(postgres_pool)


type ProfileModerationServiceDependency = Annotated[
    ProfileModerationService,
    Depends(get_profile_moderation_service),
]


def get_self_delete_service(postgres_pool: PostgresPoolDependency) -> SelfDeleteService:
    """создать service self-delete с общими транзакционными репозиториями"""

    return SelfDeleteService(postgres_pool)


type SelfDeleteServiceDependency = Annotated[
    SelfDeleteService,
    Depends(get_self_delete_service),
]


def get_staff_service(postgres_pool: PostgresPoolDependency) -> StaffService:
    """создать сервис администрирования"""

    return StaffService(postgres_pool)


type StaffServiceDependency = Annotated[
    StaffService,
    Depends(get_staff_service),
]


async def get_current_user(request: Request) -> UserRecord:
    """вернуть сотрудника по сессионному токену из cookie"""

    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        raise HTTPException(status_code=401, detail="not authorized")

    user = await UsersRepository(request.app.state.postgres_pool).get_user_by_session(
        hash_token(token)
    )
    if user is None:
        raise HTTPException(status_code=401, detail="invalid session")

    return user


async def get_optional_current_user(request: Request) -> UserRecord | None:
    """вернуть пользователя, но оставить anyone-публикации анонимными"""

    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        return None
    return await UsersRepository(request.app.state.postgres_pool).get_user_by_session(
        hash_token(token)
    )


async def require_csrf(request: Request) -> None:
    """проверить csrf-токен для изменения staff-состояния"""

    # cookie приходит с браузером, а заголовок добавляет только наш клиент
    cookie_token = request.cookies.get(CSRF_COOKIE)
    header_token = request.headers.get(CSRF_HEADER)
    if (
        cookie_token is None
        or header_token is None
        or not compare_digest(cookie_token, header_token)
    ):
        raise HTTPException(status_code=403, detail="csrf validation failed")


type CurrentUserDependency = Annotated[
    UserRecord,
    Depends(get_current_user),
]

type OptionalCurrentUserDependency = Annotated[
    UserRecord | None,
    Depends(get_optional_current_user),
]


async def get_moderator_user(user: CurrentUserDependency) -> UserRecord:
    """вернуть сотрудника с правами модератора"""

    if user.role not in {"owner", "admin", "moderator"}:
        raise HTTPException(status_code=403, detail="moderator access required")

    return user


async def get_admin_user(user: CurrentUserDependency) -> UserRecord:
    """вернуть сотрудника с правами администратора"""

    if user.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="admin access required")

    return user


async def get_owner_user(user: CurrentUserDependency) -> UserRecord:
    """вернуть владельца движка"""

    if user.role != "owner":
        raise HTTPException(status_code=403, detail="owner access required")

    return user


type ModeratorUserDependency = Annotated[
    UserRecord,
    Depends(get_moderator_user),
]

type AdminUserDependency = Annotated[
    UserRecord,
    Depends(get_admin_user),
]

type OwnerUserDependency = Annotated[
    UserRecord,
    Depends(get_owner_user),
]


async def check_ip_bans(
    request: Request,
    ip_bans_repository: Annotated[IpBansRepository, Depends(get_ip_bans_repository)],
) -> None:
    """забаненные IP не могут публиковать треды и посты"""

    ip = get_client_ip(request)
    if ip and await ip_bans_repository.get_by_ip(ip) is not None:
        raise HTTPException(status_code=403, detail="ip is banned")


type IpBansRepositoryDependency = Annotated[
    IpBansRepository,
    Depends(get_ip_bans_repository),
]


def get_spam_service(
    redis_client: RedisClientDependency,
    site_settings: SiteSettingsDependency,
) -> SpamService:
    """создать сервис защиты от спама"""

    return SpamService(redis_client, site_settings)


type SpamServiceDependency = Annotated[
    SpamService,
    Depends(get_spam_service),
]


def get_media_upload_service(
    request: Request,
    postgres_pool: PostgresPoolDependency,
    redis_client: RedisClientDependency,
    site_settings: SiteSettingsDependency,
) -> MediaUploadService:
    """создать upload service с закрытым storage из настроек процесса"""

    settings = get_settings()
    return MediaUploadService(
        postgres_pool,
        redis_client,
        site_settings,
        request.app.state.media_storage_root,
        FFmpegMediaProcessor(settings.ffmpeg_bin, settings.ffprobe_bin),
        VideoProcessor(settings.ffmpeg_bin, settings.ffprobe_bin),
        quota=request.app.state.media_quota,
    )


type MediaUploadServiceDependency = Annotated[
    MediaUploadService,
    Depends(get_media_upload_service),
]


def get_media_delivery_service(
    postgres_pool: PostgresPoolDependency,
) -> MediaDeliveryService:
    """создать read-only service проверки media перед nginx internal redirect"""

    return MediaDeliveryService(MediaDeliveryRepository(postgres_pool))


type MediaDeliveryServiceDependency = Annotated[
    MediaDeliveryService,
    Depends(get_media_delivery_service),
]
