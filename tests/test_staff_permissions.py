"""проверка границ staff-доступа"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.dependencies import (
    get_admin_user,
    get_moderator_user,
    get_owner_user,
)
from app.api.v1.boards import router as boards_router
from app.repositories.users import UserRecord
from app.schemas.threads import ThreadCreate


def make_user(role: str) -> UserRecord:
    """создать сотрудника для проверки прав"""

    return UserRecord(
        id=1,
        login="tester",
        role=role,
        created_at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_role_dependencies_allow_expected_roles() -> None:
    """роли получают только предусмотренный уровень доступа"""

    moderator = make_user("moderator")
    admin = make_user("admin")
    owner = make_user("owner")

    assert await get_moderator_user(moderator) == moderator
    assert await get_admin_user(admin) == admin
    assert await get_admin_user(owner) == owner
    assert await get_owner_user(owner) == owner


@pytest.mark.anyio
async def test_role_dependencies_reject_insufficient_role() -> None:
    """модератор не получает права администратора или владельца"""

    moderator = make_user("moderator")
    admin = make_user("admin")

    with pytest.raises(HTTPException, match="admin access required"):
        await get_admin_user(moderator)

    with pytest.raises(HTTPException, match="owner access required"):
        await get_owner_user(admin)


def test_public_board_router_has_no_mutation_routes() -> None:
    """анонимный маршрут досок не создаёт и не изменяет доски"""

    root_routes = [
        route
        for route in boards_router.routes
        if isinstance(route, APIRoute) and route.path == "/boards"
    ]

    assert len(root_routes) == 1
    assert root_routes[0].methods == {"GET"}


def test_public_thread_create_rejects_sticky_flag() -> None:
    """анонимный запрос не может передать is_sticky"""

    with pytest.raises(ValidationError):
        ThreadCreate.model_validate(
            {
                "subject": "тест",
                "is_sticky": True,
            }
        )
