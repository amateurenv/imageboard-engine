"""sql запросы для досок"""

from dataclasses import dataclass

from asyncpg import Pool

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


@dataclass(frozen=True, slots=True)
class BoardRecord:
    """данные одной доски, полученные из postgresql"""

    slug: str
    title: str
    description: str
    is_nsfw: bool


class BoardsRepository:
    """чтение досок из postgresql"""

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