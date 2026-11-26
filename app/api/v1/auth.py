"""маршруты авторизации сотрудников"""

from fastapi import APIRouter, Request, Response

from app.api.dependencies import CurrentUserDependency, UsersServiceDependency, get_client_ip
from app.core.config import get_settings
from app.core.security import SESSION_COOKIE, SESSION_TTL_SECONDS
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
        get_client_ip(request),
    )

    # в production браузер отправляет сессию только по https
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        samesite="lax",
        secure=get_settings().app_env == "production",
    )

    return user


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    users_service: UsersServiceDependency,
) -> None:
    """выйти, удалить сессию"""

    token = request.cookies.get(SESSION_COOKIE)
    if token is not None:
        await users_service.logout(token)

    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserPublic)
async def me(user: CurrentUserDependency) -> UserPublic:
    """данные текущего сотрудника"""

    return UserPublic(
        id=user.id,
        login=user.login,
        role=user.role,
        created_at=user.created_at,
    )
