"""авторизация подписок и доставка записей outbox в redis"""

import asyncio
import logging
from datetime import UTC, datetime

from asyncpg import Pool, PostgresError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.repositories.realtime import (
    RealtimeRepository,
    delete_published_events,
    lock_pending_events,
    read_event,
)
from app.ws.manager import Client, Subscription, WebSocketManager

logger = logging.getLogger(__name__)

BATCH_SIZE = 32
POLL_SECONDS = 0.5
PUBLISH_TIMEOUT = 2
BACKLOG_WARNING_SECONDS = 30


async def subscribe(
    manager: WebSocketManager, client: Client, repository: RealtimeRepository, scope: str
) -> bool:
    """проверить публичную видимость, учитывая отзыв во время запроса"""

    kind, identifier = scope.split(":", 1)
    thread_id = int(identifier) if kind == "thread" else None
    if thread_id is not None and thread_id > 2**63 - 1:
        return False
    version = manager.visibility_version
    board_id = (
        await repository.get_board_id(identifier)
        if thread_id is None else await repository.get_thread_board_id(thread_id)
    )
    if (
        board_id is None or client.close is not None
        or version != manager.visibility_version
    ):
        return False
    client.subscriptions[scope] = Subscription(board_id, thread_id)
    return True


async def publish_pending_events(pool: Pool, redis: Redis, channel: str) -> None:
    """публиковать уже сохранённые события и повторять пачку после сбоя"""

    delay = POLL_SECONDS
    warning_at = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with pool.acquire(timeout=PUBLISH_TIMEOUT) as connection:
                async with connection.transaction():
                    rows = await lock_pending_events(connection, BATCH_SIZE)
                    if rows:
                        age = (datetime.now(UTC) - rows[0]["created_at"]).total_seconds()
                        if age >= BACKLOG_WARNING_SECONDS and loop.time() >= warning_at:
                            logger.warning("возраст очереди realtime %.0f с", age)
                            warning_at = loop.time() + BACKLOG_WARNING_SECONDS
                        # проверяем наличие как минимум одного подписчика до снятия записи
                        async with asyncio.timeout(PUBLISH_TIMEOUT):
                            for row in rows:
                                subscribers = await redis.publish(
                                    channel, read_event(row).model_dump_json()
                                )
                                if subscribers == 0:
                                    raise RedisError("realtime has no redis listener")
                        await delete_published_events(connection, [row["id"] for row in rows])
        except (OSError, PostgresError, RedisError, TimeoutError) as error:
            logger.warning("публикация realtime будет повторена: %s", type(error).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)
        else:
            delay = POLL_SECONDS
            if len(rows) < BATCH_SIZE:
                await asyncio.sleep(POLL_SECONDS)
