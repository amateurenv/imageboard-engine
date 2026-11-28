"""локальные подписки и восстановление подписчика redis"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schemas.realtime import RealtimeEvent

logger = logging.getLogger(__name__)

MAX_CONNECTIONS = 100
MAX_SCOPES_PER_CONNECTION = 32
MAX_PENDING_MESSAGES = 64
MAX_EVENT_BYTES = 256
REDIS_PING_INTERVAL = 5
REDIS_TIMEOUT = 3


@dataclass(frozen=True, slots=True)
class Close:
    """код и короткая причина закрытия соединения"""

    code: int
    reason: str


FEED_UNAVAILABLE: Final = Close(1013, "realtime unavailable")


@dataclass(frozen=True, slots=True)
class Subscription:
    """проверенная область, привязанная к стабильному id доски"""

    board_id: int
    thread_id: int | None


@dataclass(eq=False, slots=True)
class Client:
    """области, ограниченная очередь и сигнал закрытия одного клиента"""

    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    messages: asyncio.Queue[dict[str, str]] = field(
        default_factory=lambda: asyncio.Queue(MAX_PENDING_MESSAGES)
    )
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    close: Close | None = None


class WebSocketManager:
    """рассылать уведомления локальным подписчикам без ожидания сокета"""

    def __init__(
        self, redis_client: Redis, channel: str, max_connections: int = MAX_CONNECTIONS
    ) -> None:
        self._redis = redis_client
        self._channel = channel
        self._max_connections = max_connections
        self._clients: set[Client] = set()
        self.ready = asyncio.Event()
        self.visibility_version = 0

    def connect(self) -> Client | None:
        """зарезервировать место без ожидания handshake"""

        if not self.ready.is_set() or len(self._clients) >= self._max_connections:
            return None
        client = Client()
        self._clients.add(client)
        return client

    def disconnect(self, client: Client) -> None:
        """освободить соединение после остановки его задач"""

        self._clients.discard(client)
        client.subscriptions.clear()

    def close_client(self, client: Client, close: Close) -> None:
        """попросить маршрут закрыть сокет и прекратить рассылку клиенту"""

        if client.close is None:
            client.close = close
            client.subscriptions.clear()
            client.stopped.set()
            logger.info("закрытие realtime клиента, код %d, причина %s", close.code, close.reason)

    def enqueue(self, client: Client, message: dict[str, str]) -> None:
        """поставить кадр в очередь или отключить медленного клиента"""

        if client.close is not None:
            return
        try:
            client.messages.put_nowait(message)
        except asyncio.QueueFull:
            self.close_client(client, Close(1013, "slow client"))

    def deliver(self, event: RealtimeEvent) -> None:
        """уведомить подписчиков и отозвать области, потерявшие публичный доступ"""

        if event.unavailable:
            self.visibility_version += 1

        for client in self._clients:
            for scope, target in tuple(client.subscriptions.items()):
                if target.board_id != event.board_id:
                    continue
                if target.thread_id is not None and event.thread_id not in (
                    None, target.thread_id
                ):
                    continue
                revoke = event.unavailable and (
                    event.thread_id is None or target.thread_id is not None
                )
                self.enqueue(
                    client, {"type": "unavailable" if revoke else "changed", "scope": scope}
                )
                if revoke:
                    client.subscriptions.pop(scope, None)

    def _feed_lost(self) -> None:
        """потребовать новое rest-чтение у клиентов после возможного пропуска"""

        self.ready.clear()
        self.visibility_version += 1
        for client in self._clients:
            self.close_client(client, FEED_UNAVAILABLE)

    async def consume_redis(self) -> None:
        """пересоздавать подписку с ограниченной задержкой при сбое"""

        delay = 0.25
        try:
            while True:
                try:
                    await self._listen()
                except (RedisError, OSError, TimeoutError) as error:
                    was_ready = self.ready.is_set()
                    self._feed_lost()
                    logger.warning("подписка redis realtime потеряна: %s", type(error).__name__)
                    if was_ready:
                        delay = 0.25
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5)
        finally:
            self._feed_lost()

    async def _listen(self) -> None:
        """дождаться подтверждения подписки и проверять ответы redis на ping"""

        loop = asyncio.get_running_loop()
        async with self._redis.pubsub() as pubsub:
            await asyncio.wait_for(pubsub.subscribe(self._channel), REDIS_TIMEOUT)
            subscribed = False
            awaiting_pong = False
            deadline = loop.time() + REDIS_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if not subscribed or awaiting_pong:
                        raise TimeoutError
                    await asyncio.wait_for(pubsub.ping("realtime"), REDIS_TIMEOUT)
                    awaiting_pong = True
                    deadline = loop.time() + REDIS_TIMEOUT
                    continue
                message = await asyncio.wait_for(
                    pubsub.get_message(timeout=remaining), remaining + 0.5
                )
                if message is None:
                    continue
                if message["type"] == "subscribe":
                    # redis-py может восстановить подписку внутри get_message
                    if subscribed:
                        logger.warning("redis realtime переподписался с возможным пропуском")
                        self._feed_lost()
                    subscribed = True
                    awaiting_pong = False
                    deadline = loop.time() + REDIS_PING_INTERVAL
                    self.ready.set()
                    logger.info("подписка redis realtime подтверждена")
                elif message["type"] == "pong" and message["data"] == "realtime" and awaiting_pong:
                    awaiting_pong = False
                    deadline = loop.time() + REDIS_PING_INTERVAL
                elif message["type"] == "message" and subscribed:
                    self._receive_event(message["data"])

    def _receive_event(self, data: object) -> None:
        """пропустить только короткие уведомления со строгой схемой"""

        if not isinstance(data, (str, bytes)) or len(data) > MAX_EVENT_BYTES:
            return
        try:
            event = RealtimeEvent.model_validate_json(data)
        except ValidationError:
            logger.warning("redis realtime вернул событие неверного формата")
            return
        self.deliver(event)
