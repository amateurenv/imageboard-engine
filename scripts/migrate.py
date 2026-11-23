"""запуск sql миграций"""

import asyncio
import hashlib
import re
from pathlib import Path

import asyncpg

from app.core.config import get_settings

# корень проекта - папка на два уровня выше этого файла
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# папка с sql миграциями
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# имя миграции должно начинаться с номера, например 001_initial_schema.sql
MIGRATION_NAME = re.compile(r"^\d{3,}_[a-z0-9_]+\.sql$")

# один и тот же ключ не даст двум процессам менять базу одновременно
MIGRATION_LOCK_ID = 836_251_904


def get_checksum(path: Path) -> str:
    """получить отпечаток содержимого sql файла"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_migration_files() -> list[Path]:
    """найти и проверить все sql миграции"""

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    for path in migration_files:
        if not MIGRATION_NAME.fullmatch(path.name):
            message = (
                f"неверное имя миграции: {path.name}\n"
                "пример правильного имени: 001_initial_schema.sql"
            )
            raise RuntimeError(message)

    return migration_files


async def run_migrations() -> None:
    """применить новые миграции по порядку"""

    # берём DATABASE_URL из .env
    settings = get_settings()

    # этому разовому скрипту не нужен пул, достаточно одного подключения
    connection = await asyncpg.connect(dsn=str(settings.database_url))

    try:
        # блокировка защищает от одновременного запуска миграций
        await connection.fetchval("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)

        try:
            # таблица хранит список уже применённых миграций
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )

            # читаем миграции, которые база уже знает
            applied_rows = await connection.fetch(
                "SELECT migration_id, checksum FROM schema_migrations"
            )

            applied_checksums = {
                row["migration_id"]: row["checksum"]
                for row in applied_rows
            }

            for path in get_migration_files():
                migration_id = path.stem
                checksum = get_checksum(path)
                applied_checksum = applied_checksums.get(migration_id)

                # миграция уже была применена
                if applied_checksum is not None:
                    # менять старый sql файл нельзя, иначе история базы станет разной
                    if applied_checksum != checksum:
                        raise RuntimeError(
                            f"содержимое уже применённой миграции изменилось: {migration_id}"
                        )

                    print(f"skip  {migration_id}")
                    continue

                # читаем новый sql файл
                sql = path.read_text(encoding="utf-8")

                if not sql.strip():
                    raise RuntimeError(f"пустая миграция: {migration_id}")

                # sql и запись в журнал выполняются как одна операция
                async with connection.transaction():
                    await connection.execute(sql)

                    await connection.execute(
                        """
                        INSERT INTO schema_migrations (migration_id, checksum)
                        VALUES ($1, $2)
                        """,
                        migration_id,
                        checksum,
                    )

                print(f"apply {migration_id}")
        finally:
            # освобождаем блокировку после работы
            await connection.fetchval("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)
    finally:
        # закрываем единственное подключение скрипта
        await connection.close()


if __name__ == "__main__":
    # этот блок запускается только при команде python -m scripts.migrate
    asyncio.run(run_migrations())