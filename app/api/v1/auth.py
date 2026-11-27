"""маршруты авторизации сотрудников"""

from secrets import token_urlsafe

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.dependencies import (
    CurrentUserDependency,
    ProfilesServiceDependency,
    UsersServiceDependency,
    get_client_ip,
    require_csrf,
)
from app.core.config import get_settings
from app.core.security import CSRF_COOKIE, SESSION_COOKIE, SESSION_TTL_SECONDS, hash_password
from app.schemas.users import AccountPublic, LoginSchema, RegisterSchema, UserPublic
from app.services.profiles import RegistrationError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AccountPublic, status_code=201)
async def register(
    registration: RegisterSchema,
    profiles_service: ProfilesServiceDependency,
) -> AccountPublic:
    """создать обычный аккаунт и его личную доску"""

    try:
        return await profiles_service.register(
            registration.handle,
            hash_password(registration.password),
        )
    except RegistrationError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "handle_taken", "message": str(error)},
        ) from error


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
    # клиент читает этот токен и возвращает его в заголовке следующего запроса
    response.set_cookie(
        CSRF_COOKIE,
        token_urlsafe(32),
        httponly=False,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        samesite="strict",
        secure=get_settings().app_env == "production",
    )

    return user


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    users_service: UsersServiceDependency,
    csrf_checked: None = Depends(require_csrf),
) -> None:
    """выйти, удалить сессию"""

    token = request.cookies.get(SESSION_COOKIE)
    if token is not None:
        await users_service.logout(token)

    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/me", response_model=UserPublic)
async def me(user: CurrentUserDependency) -> UserPublic:
    """данные текущего сотрудника"""

    return UserPublic(
        id=user.id,
        login=user.login,
        role=user.role,
        created_at=user.created_at,
    )
