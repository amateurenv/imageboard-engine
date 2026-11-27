"""правила администрирования: доски, треды, посты, настройки, баны"""

from dataclasses import asdict

from asyncpg import Pool

from app.core.site_settings import SiteSettings
from app.core.site_settings_repository import save_site_settings
from app.repositories.boards import BoardDetailRecord, BoardsRepository
from app.repositories.threads import ThreadsRepository
from app.repositories.users import UsersRepository
from app.schemas.boards import BoardCreate, BoardManagement, BoardMediaOverrides, BoardUpdate
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

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # удаление и запись в журнал должны сохраниться вместе
                board_id = await self._boards_repository.delete_board_with_threads_in_transaction(
                    connection, slug
                )
                if board_id is None:
                    return False
                await self._users_repository.log_moderation_in_transaction(
                    connection, actor_id, "delete_board", "board", board_id,
                    reason=f"board deleted: {slug}",
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
            media_overrides=board_create.media_overrides.model_dump(exclude_none=True),
        )

        return self._to_management(board)

    async def update_board(
        self, slug: str, board_update: BoardUpdate
    ) -> BoardManagement | None:
        """изменить доску, None если такая доска не найдена"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # блокировка ждёт другой PATCH и читает уже свежие поля
                current = await self._boards_repository.get_by_slug_for_update(connection, slug)
                if current is None:
                    return None
                values = asdict(current)
                values.update(board_update.model_dump(exclude_none=True))
                updated = await self._boards_repository.update_board_in_transaction(
                    connection, **values
                )
        return None if updated is None else self._to_management(updated)

    async def update_thread(
        self, thread_id: int, update: ThreadUpdateAdmin
    ) -> ThreadDetail | None:
        """изменить тред из админки"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # так два частичных PATCH не затрут изменения друг друга
                current = await self._threads_repository.get_thread_for_update(
                    connection, thread_id
                )
                if current is None:
                    return None
                values = asdict(current)
                values.update(update.model_dump(exclude_none=True))
                updated = await self._threads_repository.update_thread_in_transaction(
                    connection,
                    thread_id=thread_id,
                    subject=values["subject"],
                    is_sticky=values["is_sticky"],
                    is_locked=values["is_locked"],
                    is_archived=values["is_archived"],
                )
        if updated is None:
            return None
        return ThreadDetail(
            id=updated.id,
            subject=updated.subject,
            is_sticky=updated.is_sticky,
            is_locked=updated.is_locked,
            is_archived=updated.is_archived,
            created_at=updated.created_at,
            bumped_at=updated.bumped_at,
        )

    async def delete_thread(
        self, actor_id: int, thread_id: int, reason: str | None = None
    ) -> None:
        """удалить тред и все его посты, записать в журнал"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # при ошибке журнала транзакция откатит и удаление треда
                if not await self._threads_repository.delete_thread_in_transaction(
                    connection, thread_id
                ):
                    return
                await self._users_repository.log_moderation_in_transaction(
                    connection, actor_id, "delete_thread", "thread", thread_id, reason=reason
                )

    async def delete_post(
        self, actor_id: int, post_id: int, reason: str | None = None
    ) -> None:
        """удалить пост, записать в журнал"""

        async with self._postgres_pool.acquire() as connection:
            async with connection.transaction():
                # первый пост задаёт смысл треда, его удаляют только вместе с тредом
                if await self._threads_repository.is_opening_post_in_transaction(
                    connection, post_id
                ):
                    raise ValueError("opening post must be removed with its thread")
                if not await self._threads_repository.delete_non_opening_post_in_transaction(
                    connection, post_id
                ):
                    return
                await self._users_repository.log_moderation_in_transaction(
                    connection, actor_id, "delete_post", "post", post_id, reason=reason
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
            media_overrides=BoardMediaOverrides.model_validate(board.media_overrides),
            telegram_sync_warning=self._telegram_sync_warning(board.media_overrides),
        )

    @staticmethod
    def _telegram_sync_warning(media_overrides: dict[str, object]) -> str | None:
        """предупредить о лимите, который обычный Telegram Bot API не заберёт"""

        attachment_count = media_overrides.get("max_attachments_per_post", 10)
        attachment_size = media_overrides.get("max_attachment_size_mib", 20)
        total_size = media_overrides.get("max_total_size_mib", 200)
        if isinstance(attachment_count, int) and attachment_count > 10:
            return "Telegram поддерживает не больше 10 вложений в одном альбоме"
        if isinstance(attachment_size, int) and attachment_size > 20:
            return "Telegram getFile не скачивает медиа больше 20 MiB"
        if isinstance(total_size, int) and total_size > 200:
            return "пост тяжелее 200 MiB может перегрузить синхронизацию"
        return None
