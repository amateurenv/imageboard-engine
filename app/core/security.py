"""безопасность: пароли, сессионные токены и куки"""

import hashlib
import secrets

import bcrypt

# имя куки сессионного токена
SESSION_COOKIE = "session_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"

# сколько дней живёт сессия
SESSION_TTL_SECONDS = 30 * 24 * 3600


def hash_password(password: str) -> str:
    """хешировать пароль"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """проверить пароль по хешу"""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def generate_token() -> str:
    """сгенерировать новый сессионный токен"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """хешировать сессионный токен"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
