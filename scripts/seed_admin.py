"""создать первого staff-администратора, если ещё нет пользователя с ролью owner"""

import asyncio
import getpass
import re
import sys

import asyncpg

from app.core.config import get_settings
from app.core.security import hash_password

# допустимые символы login и его длина (повторяет CHECK в migrations/003_staff.sql)
LOGIN_PATTERN = re.compile(r"^[a-z0-9_]{3,30}$")

# минимальная длина пароля
MIN_PASSWORD_LENGTH = 8


async def main() -> None:
    """создать первого staff-администратора"""

    login = sys.argv[1] if len(sys.argv) > 1 else input("login: ")
    password = sys.argv[2] if len(sys.argv) > 2 else getpass.getpass("password: ")

    if not LOGIN_PATTERN.match(login):
        print("login должен состоять из 3-30 символов [a-z0-9_]")
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"пароль должен быть длиннее {MIN_PASSWORD_LENGTH - 1} символов")
        return

    settings = get_settings()

    connection = await asyncpg.connect(dsn=str(settings.database_url))
    try:
        if await connection.fetchval("SELECT id FROM users WHERE login = $1", login):
            print("пользователь уже существует")
            return
        if await connection.fetchval("SELECT id FROM users WHERE role = 'owner' LIMIT 1"):
            print("уже есть пользователь с ролью owner")
            return

        await connection.execute(
            "INSERT INTO users (login, password_hash, role) VALUES ($1, $2, 'owner')",
            login,
            hash_password(password),
        )
        print(f"создан пользователь owner: {login}")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
