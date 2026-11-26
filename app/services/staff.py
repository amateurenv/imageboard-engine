"""правила администрирования: доски, треды, посты, настройки, баны"""

from dataclasses import asdict

from asyncpg import Pool

from app.core.site_settings import SiteSettings
from app.core.site_settings_repository import save_site_settings
from app.repositories.boards import BoardDetailRecord, BoardsRepository
from app.repositories.threads import ThreadsRepository
from app.repositories.users import UsersRepository
from app.schemas.boards import BoardCreate, BoardManagement, BoardUpdate
from app.schemas.staff import ThreadUpdateAdmin
from app.schemas.threads import ThreadDetail


class StaffService:
    """администрирование досок, тредов, постов, настроек и банов"""

    def __init__(self, postgres_pool: Pool) -> None:
        # создаём репозитории на общем пуле, новых подключений не открываем
        self._boards_repository = BoardsRepository(postgres_pool)
        self._threads_repository = ThreadsRepository(postgres_pool)
        self._users_repository = UsersRepository(postgres_pool)
        self._postgres_pool = postgres_pool

    async def list_boards(self) -> list[BoardManagement]:
        """все доски с полными данными (включая неактивные)"""

        return [
            self._to_management(board)
            for board in await self._boards_repository.list_all_boards()
        ]

    async def delete_board(self, actor_id: int, slug: str) -> bool:
        """удалить доску и все её треды, записать действие в журнал"""

        board_id = await self._boards_repository.delete_board_with_threads(slug)
        if board_id is None:
            return False

        # удаление тредов и доски уже завершилось одной транзакцией repository
        await self._users_repository.log_moderation(
            actor_id, "delete_board", "board", board_id, reason=f"board deleted: {slug}"
        )
        return True

    async def create_board(self, board_create: BoardCreate) -> BoardManagement:
        """создать доску и вернуть её данные"""

        board = await self._boards_repository.create_board(
            slug=board_create.slug,
            title=board_create.title,
            description=board_create.description,
            position=board_create.position,
            is_active=board_create.is_active,
            is_nsfw=board_create.is_nsfw,
        )

        return self._to_management(board)

    async def update_board(
        self, slug: str, board_update: BoardUpdate
    ) -> BoardManagement | None:
        """изменить доску, None если такая доска не найдена"""

        current = await self._boards_repository.get_by_slug(slug)
        if current is None:
            return None

        # None в запросе значит «не менять», мержим с текущими значениями
        values = asdict(current)
        values.update(board_update.model_dump(exclude_none=True))

        updated = await self._boards_repository.update_board(**values)
        if updated is None:
            return None

        return self._to_management(updated)

    async def update_thread(
        self, thread_id: int, update: ThreadUpdateAdmin
    ) -> ThreadDetail | None:
        """изменить тред из админки"""

        current = await self._threads_repository.get_thread(thread_id)
        if current is None:
            return None

        # None в запросе значит «не менять», мержим с текущими значениями
        values = asdict(current)
        values.update(update.model_dump(exclude_none=True))

        return await self._threads_repository.update_thread(
            thread_id=thread_id,
            subject=values["subject"],
            is_sticky=values["is_sticky"],
            is_locked=values["is_locked"],
            is_archived=values["is_archived"],
        )

    async def delete_thread(
        self, actor_id: int, thread_id: int, reason: str | None = None
    ) -> None:
        """удалить тред и все его посты, записать в журнал"""

        if await self._threads_repository.delete_thread(thread_id):
            await self._users_repository.log_moderation(
                actor_id, "delete_thread", "thread", thread_id, reason=reason
            )

    async def delete_post(
        self, actor_id: int, post_id: int, reason: str | None = None
    ) -> None:
        """удалить пост, записать в журнал"""

        if await self._threads_repository.delete_post(post_id):
            await self._users_repository.log_moderation(
                actor_id, "delete_post", "post", post_id, reason=reason
            )

    async def save_settings(self, settings: SiteSettings) -> None:
        """сохранить группы настроек в базу"""

        await save_site_settings(self._postgres_pool, settings)

    def _to_management(self, board: BoardDetailRecord) -> BoardManagement:
        """запись репозитория в схему ответа админки"""

        return BoardManagement(
            slug=board.slug,
            title=board.title,
            description=board.description,
            position=board.position,
            is_active=board.is_active,
            is_nsfw=board.is_nsfw,
        )
