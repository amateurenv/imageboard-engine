"""owner-модерация profile-досок с общей staff-проверкой и журналом"""

# ruff: noqa: E501 - SQL и транзакционные вызовы читаются целиком проще

from asyncpg import Pool

from app.repositories.users import UserRecord, UsersRepository

STAFF_ROLES = {"owner", "admin", "moderator"}


class ProfileModerationError(PermissionError):
    """цель не принадлежит profile-доске актора"""


class ProfileModerationService:
    """менять только состояние сущностей, не текст чужих сообщений"""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._users = UsersRepository(pool)

    async def update_thread(
        self, handle: str, thread_id: int, actor: UserRecord, **changes: bool
    ) -> bool:
        """закрепить, закрыть или скрыть тред своей profile-доски"""

        return await self._change_thread(handle, thread_id, actor, changes, "update_thread")

    async def delete_thread(self, handle: str, thread_id: int, actor: UserRecord) -> bool:
        """удалить тред по той же каскадной семантике, что и staff"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                board = await self._authorize(connection, handle, actor, thread_id, None)
                if board is None:
                    return False
                deleted = await connection.fetchval("DELETE FROM threads WHERE id = $1 RETURNING id", thread_id)
                if deleted is None:
                    return False
                await self._log(connection, actor, "delete_thread", "thread", thread_id, board)
        return True

    async def update_post(self, handle: str, post_id: int, actor: UserRecord, hidden: bool) -> bool:
        """скрыть или вернуть ответ без редактирования содержимого"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                board = await self._authorize(connection, handle, actor, None, post_id)
                if board is None:
                    return False
                changed = await connection.fetchval(
                    "UPDATE posts SET is_hidden = $2 WHERE id = $1 RETURNING id", post_id, hidden
                )
                if changed is None:
                    return False
                await self._log(connection, actor, "hide_post" if hidden else "unhide_post", "post", post_id, board)
        return True

    async def delete_post(self, handle: str, post_id: int, actor: UserRecord) -> bool:
        """удалить только не-OP ответ, сохраняя правило существующего staff api"""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                board = await self._authorize(connection, handle, actor, None, post_id)
                if board is None:
                    return False
                deleted = await connection.fetchval(
                    """WITH target AS (SELECT id, thread_id FROM posts WHERE id=$1 FOR UPDATE),
                    op AS (SELECT id FROM posts WHERE thread_id=(SELECT thread_id FROM target) ORDER BY id LIMIT 1)
                    DELETE FROM posts WHERE id=(SELECT id FROM target) AND id<>(SELECT id FROM op) RETURNING id""",
                    post_id,
                )
                if deleted is None:
                    raise ValueError("opening post must be removed with its thread")
                await self._log(connection, actor, "delete_post", "post", post_id, board)
        return True

    async def _change_thread(self, handle: str, thread_id: int, actor: UserRecord, changes: dict[str, bool], action: str) -> bool:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                board = await self._authorize(connection, handle, actor, thread_id, None)
                if board is None:
                    return False
                current = await connection.fetchrow("SELECT is_sticky,is_locked,is_hidden FROM threads WHERE id=$1 FOR UPDATE", thread_id)
                if current is None:
                    return False
                values = {key: changes.get(key, current[key]) for key in ("is_sticky", "is_locked", "is_hidden")}
                await connection.execute("UPDATE threads SET is_sticky=$2,is_locked=$3,is_hidden=$4 WHERE id=$1", thread_id, values["is_sticky"], values["is_locked"], values["is_hidden"])
                await self._log(connection, actor, action, "thread", thread_id, board, values)
        return True

    async def _authorize(self, connection: object, handle: str, actor: UserRecord, thread_id: int | None, post_id: int | None) -> object | None:
        query = "SELECT b.id,b.board_type,b.owner_user_id FROM boards b WHERE b.slug=$1"
        params: tuple[object, ...] = (handle,)
        if thread_id is not None:
            query = "SELECT b.id,b.board_type,b.owner_user_id FROM boards b JOIN threads t ON t.board_id=b.id WHERE b.slug=$1 AND t.id=$2"
            params = (handle, thread_id)
        if post_id is not None:
            query = "SELECT b.id,b.board_type,b.owner_user_id FROM boards b JOIN threads t ON t.board_id=b.id JOIN posts p ON p.thread_id=t.id WHERE b.slug=$1 AND p.id=$2"
            params = (handle, post_id)
        board = await connection.fetchrow(query, *params)  # type: ignore[attr-defined]
        if board is None:
            return None
        if actor.role in STAFF_ROLES:
            return board
        if board["board_type"] != "profile" or board["owner_user_id"] != actor.id:
            raise ProfileModerationError("profile moderation forbidden")
        return board

    async def _log(self, connection: object, actor: UserRecord, action: str, target_type: str, target_id: int, board: object, values: dict[str, bool] | None = None) -> None:
        await self._users.log_moderation_in_transaction(connection, actor.id, action, target_type, target_id, metadata={"actor_type": "staff" if actor.role in STAFF_ROLES else "profile_owner", "board_id": board["id"], "changes": values or {}})  # type: ignore[index]
