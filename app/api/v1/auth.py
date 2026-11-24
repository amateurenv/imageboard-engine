"""маршруты авторизации сотрудников"""

from fastapi import APIRouter, Request, Response

from app.api.dependencies import CurrentUserDependency, UsersServiceDependency
from app.core.security import SESSION_COOKIE
from app.schemas.users import LoginSchema, UserPublic

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserPublic)
async def login(
    request: Request,
    login_schema: LoginSchema,
    response: Response,
    users_service: UsersServiceDependency,
) -> UserPublic:
    """войти по login и паролю, установить сессионную cookie"""

    user, token = await users_service.login(
        login_schema.login,
        login_schema.password,
        request.client.host or "",
    )

    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")

    return user


@router.post("/logout", status_code=204)
async def logout(request: Request, users_service: UsersServiceDependency) -> None:
    """выйти, удалить сессию"""

    token = request.cookies.get(SESSION_COOKIE)
    if token is not None:
        await users_service.logout(token)


@router.get("/me", response_model=UserPublic)
async def me(user: CurrentUserDependency) -> UserPublic:
    """данные текущего сотрудника"""

    return UserPublic(
        id=user.id,
        login=user.login,
        role=user.role,
        created_at=user.created_at,
    )
