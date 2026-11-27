"""безопасное self-delete без выдачи прав owner или staff"""

from dataclasses import dataclass

from asyncpg import Connection, Pool

from app.core.security import verify_password
from app.repositories.threads import (
    SelfDeletePostRecord,
    SelfDeleteThreadRecord,
    ThreadsRepository,
)
from app.repositories.users import UserRecord, UsersRepository


@dataclass(frozen=True, slots=True)
class SelfDeleteError(PermissionError):
    """стабильная ошибка удаления без деталей об авторе"""

    code: str
    status_code: int

    def __str__(self) -> str:
        return self.code


class SelfDeleteService:
    """удалять только собственный контент в одной транзакции с audit log"""

    def __init__(self, postgres_pool: Pool) -> None:
        self._pool = postgres_pool
        self._threads = ThreadsRepository(postgres_pool)
        self._users = UsersRepository(postgres_pool)

    async def delete_thread(
        self, thread_id: int, actor: UserRecord | None, deletion_password: str | None
    ) -> bool:
        """удалить тред только от имени его op автора"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                target = await self._threads.get_thread_for_self_delete(connection, thread_id)
                if target is None:
                    return False
                actor_type = self._authorize(target, actor, deletion_password)
                if not await self._threads.delete_thread_in_transaction(connection, thread_id):
                    return False
                await self._log(
                    connection, actor, actor_type, "self_delete_thread", "thread", thread_id,
                    target.board_id, thread_id, target.post_id,
                )
        return True

    async def delete_post(
        self, post_id: int, actor: UserRecord | None, deletion_password: str | None
    ) -> bool:
        """удалить только собственный не-op ответ"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                target = await self._threads.get_post_for_self_delete(connection, post_id)
                if target is None:
                    return False
                if target.is_opening:
                    raise SelfDeleteError("opening_post_requires_thread_delete", 409)
                actor_type = self._authorize(target, actor, deletion_password)
                if not await self._threads.delete_non_opening_post_in_transaction(
                    connection, post_id
                ):
                    return False
                await self._log(
                    connection, actor, actor_type, "self_delete_post", "post", post_id,
                    target.board_id, target.thread_id, post_id,
                )
        return True

    @staticmethod
    def _authorize(
        target: SelfDeleteThreadRecord | SelfDeletePostRecord,
        actor: UserRecord | None,
        deletion_password: str | None,
    ) -> str:
        """не смешивать session ownership и анонимный пароль"""

        if actor is not None:
            if target.author_user_id == actor.id:
                return "account_self"
            raise SelfDeleteError("self_delete_forbidden", 403)
        if (
            deletion_password is None
            or target.deletion_password_hash is None
            or not verify_password(deletion_password, target.deletion_password_hash)
        ):
            raise SelfDeleteError("self_delete_forbidden", 403)
        return "anonymous_self"

    async def _log(
        self,
        connection: Connection,
        actor: UserRecord | None,
        actor_type: str,
        action: str,
        target_type: str,
        target_id: int,
        board_id: int,
        thread_id: int,
        post_id: int,
    ) -> None:
        """сохранить только безопасный контекст действия без учётных данных"""

        await self._users.log_moderation_in_transaction(
            connection,
            None if actor is None else actor.id,
            action,
            target_type,
            target_id,
            metadata={
                "actor_type": actor_type,
                "board_id": board_id,
                "thread_id": thread_id,
                "post_id": post_id,
            },
        )
