"""административные маршруты для банов по IP"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import (
    IpBansRepositoryDependency,
    ModeratorUserDependency,
    require_csrf,
)
from app.repositories.ip_bans import DuplicateIpBanError, IpBanRecord
from app.schemas.staff import IpBanCreate, IpBanPublic


def _to_public(record: IpBanRecord) -> IpBanPublic:
    """запись репозитория в публичную схему"""

    return IpBanPublic(
        ip=record.ip,
        reason=record.reason,
        created_at=record.created_at,
    )


router = APIRouter(
    prefix="/admin/ip_bans",
    tags=["admin-ip-bans"],
)


@router.get("", response_model=list[IpBanPublic])
async def list_ip_bans(
    ip_bans_repository: IpBansRepositoryDependency,
    moderator_user: ModeratorUserDependency,
) -> list[IpBanPublic]:
    """список всех банов по IP"""

    return [
        _to_public(record) for record in await ip_bans_repository.list_all()
    ]


@router.post("", response_model=IpBanPublic, dependencies=[Depends(require_csrf)])
async def create_ip_ban(
    ip_ban: IpBanCreate,
    moderator_user: ModeratorUserDependency,
    ip_bans_repository: IpBansRepositoryDependency,
) -> IpBanPublic:
    """забанить IP"""

    try:
        record = await ip_bans_repository.create(
            str(ip_ban.ip),
            ip_ban.reason,
            moderator_user.id,
        )
    except DuplicateIpBanError as exc:
        raise HTTPException(status_code=409, detail="ip is already banned") from exc

    return _to_public(record)


@router.delete("/{ip}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_ip_ban(
    ip: str,
    ip_bans_repository: IpBansRepositoryDependency,
    moderator_user: ModeratorUserDependency,
) -> None:
    """снять бан"""

    await ip_bans_repository.delete(ip)
