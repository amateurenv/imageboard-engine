"""sql запросы для досок"""

from dataclasses import dataclass

from asyncpg import Pool, Record

# берём только поля, которые нужны для публичного списка досок
LIST_ACTIVE_BOARDS_SQL = """
SELECT
    slug,
    title,
    description,
    is_nsfw
FROM boards
WHERE is_active = TRUE
ORDER BY position ASC, id ASC
"""

# поля доски для полного представления в админке
BOARD_COLUMNS = "slug, title, description, position, is_active, is_nsfw"

SELECT_BY_SLUG_SQL = f"""
SELECT {BOARD_COLUMNS}
FROM boards
WHERE slug = $1
"""

CREATE_BOARD_SQL = f"""
INSERT INTO boards (slug, title, description, position, is_active, is_nsfw)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING {BOARD_COLUMNS}
"""

LIST_ALL_BOARDS_SQL = """
SELECT id, slug, title, description, position, is_active, is_nsfw
FROM boards
ORDER BY position ASC, id ASC
"""

DELETE_BOARD_SQL = """
DELETE FROM boards WHERE slug = $1
"""

UPDATE_BOARD_SQL = f"""
UPDATE boards
SET title = $2, description = $3, position = $4, is_active = $5, is_nsfw = $6
WHERE slug = $1
RETURNING {BOARD_COLUMNS}
"""


@dataclass(frozen=True, slots=True)
class BoardRecord:
    """данные одной доски, полученные из postgresql"""

    slug: str
    title: str
    description: str
    is_nsfw: bool


@dataclass(frozen=True, slots=True)
class BoardDetailRecord:
    """полные данные доски, доступные админке"""

    slug: str
    title: str
    description: str
    position: int
    is_active: bool
    is_nsfw: bool


def _detail_record(row: Record) -> BoardDetailRecord:
    """превратить строку базы в запись доски"""

    return BoardDetailRecord(
        slug=row["slug"],
        title=row["title"],
        description=row["description"],
        position=row["position"],
        is_active=row["is_active"],
        is_nsfw=row["is_nsfw"],
    )


class BoardsRepository:
    """чтение и управление досками в postgresql"""

    def __init__(self, postgres_pool: Pool) -> None:
        # сохраняем общий пул, а не открываем новое подключение
        self._postgres_pool = postgres_pool

    async def list_active_boards(self) -> list[BoardRecord]:
        """вернуть активные доски в порядке отображения"""

        # fetch возвращает все строки результата одним запросом
        rows = await self._postgres_pool.fetch(LIST_ACTIVE_BOARDS_SQL)

        return [
            BoardRecord(
                slug=row["slug"],
                title=row["title"],
                description=row["description"],
                is_nsfw=row["is_nsfw"],
            )
            for row in rows
        ]

    async def get_by_slug(self, slug: str) -> BoardDetailRecord | None:
        """вернуть доску по slug"""

        row = await self._postgres_pool.fetchrow(SELECT_BY_SLUG_SQL, slug)

        if row is None:
            return None

        return _detail_record(row)

    async def create_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
    ) -> BoardDetailRecord:
        """создать доску и вернуть запись с полными данными"""

        row = await self._postgres_pool.fetchrow(
            CREATE_BOARD_SQL, slug, title, description, position, is_active, is_nsfw
        )

        if row is None:
            # insert с параметрами не вернёт пустую строку
            raise RuntimeError(f"board not created: {slug}")

        return _detail_record(row)

    async def list_all_boards(self) -> list[BoardDetailRecord]:
        """все доски с полными данными, включая неактивные"""

        rows = await self._postgres_pool.fetch(LIST_ALL_BOARDS_SQL)
        return [_detail_record(row) for row in rows]

    async def delete_board(self, slug: str) -> None:
        """удалить доску"""

        await self._postgres_pool.execute(DELETE_BOARD_SQL, slug)

    async def update_board(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        position: int,
        is_active: bool,
        is_nsfw: bool,
    ) -> BoardDetailRecord | None:
        """изменить доску, None если такая доска не найдена"""

        row = await self._postgres_pool.fetchrow(
            UPDATE_BOARD_SQL, slug, title, description, position, is_active, is_nsfw
        )

        if row is None:
            return None

        return _detail_record(row)