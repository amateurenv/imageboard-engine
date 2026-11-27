"""безопасная основа realtime, пока маршруты остаются выключенными"""

import asyncio
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from redis.asyncio import Redis
from starlette.websockets import WebSocket

# предел на один процесс: не даём одному worker занять всю память соединениями
MAX_CONNECTIONS = 100
# клиент может смотреть несколько тредов, но не подписывать сервер на всё сразу
MAX_SCOPES_PER_CONNECTION = 32
# событие сообщает об изменении, подробности клиент дочитает через REST
MAX_EVENT_BYTES = 8 * 1024
# ping раз в 30 секунд помогает быстрее заметить оборванную сеть
HEARTBEAT_SECONDS = 30
EVENTS_CHANNEL = "ws:events"


def board_scope(slug: str) -> str:
    """собрать область событий одной доски"""

    return f"board:{slug}"


def thread_scope(thread_id: int) -> str:
    """собрать область событий одного треда"""

    return f"thread:{thread_id}"


class WebSocketManager:
    """хранить подписки текущего процесса и пересылать события через redis"""

    def __init__(self, redis_client: Redis, max_connections: int = MAX_CONNECTIONS) -> None:
        self._redis_client = redis_client
        self._max_connections = max_connections
        self._connections: set[WebSocket] = set()
        self._scopes: dict[str, set[WebSocket]] = defaultdict(set)
        self._connection_scopes: dict[WebSocket, set[str]] = defaultdict(set)
        self._stopped = asyncio.Event()

    async def connect(self, websocket: WebSocket) -> bool:
        """принять соединение, если у процесса есть свободное место"""

        if len(self._connections) >= self._max_connections:
            await websocket.close(code=1013, reason="server is busy")
            return False
        await websocket.accept()
        self._connections.add(websocket)
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        """снять все подписки отключившегося клиента"""

        self._connections.discard(websocket)
        for scope in self._connection_scopes.pop(websocket, set()):
            subscribers = self._scopes[scope]
            subscribers.discard(websocket)
            if not subscribers:
                self._scopes.pop(scope, None)

    async def subscribe(self, websocket: WebSocket, scope: str) -> bool:
        """подписать клиента на одну доску или тред"""

        if not self._is_valid_scope(scope):
            return False
        scopes = self._connection_scopes[websocket]
        if scope not in scopes and len(scopes) >= MAX_SCOPES_PER_CONNECTION:
            return False
        scopes.add(scope)
        self._scopes[scope].add(websocket)
        return True

    async def unsubscribe(self, websocket: WebSocket, scope: str) -> None:
        """снять одну подписку клиента"""

        self._connection_scopes[websocket].discard(scope)
        subscribers = self._scopes.get(scope)
        if subscribers is not None:
            subscribers.discard(websocket)
            if not subscribers:
                self._scopes.pop(scope, None)

    async def broadcast(self, event: dict[str, Any], scopes: Iterable[str] = ()) -> None:
        """отправить событие только подписчикам переданных областей"""

        unique_scopes = sorted(set(scopes))
        if not unique_scopes:
            # событие без области нельзя безопасно показать всем посетителям
            return
        payload = json.dumps({"event": event, "scopes": unique_scopes}, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("realtime event is too large")
        await self._redis_client.publish(EVENTS_CHANNEL, payload)

    async def consume_redis(self) -> None:
        """получать события redis без busy loop до штатной остановки"""

        async with self._redis_client.pubsub() as pubsub:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while not self._stopped.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None or message["type"] != "message":
                    continue
                await self._deliver(message["data"])

    async def aclose(self) -> None:
        """остановить consumer и закрыть соединения при shutdown"""

        self._stopped.set()
        for websocket in tuple(self._connections):
            await websocket.close(code=1001, reason="server is stopping")
            await self.disconnect(websocket)

    async def _deliver(self, raw_data: str | bytes) -> None:
        """разослать событие только локальным подписчикам его областей"""

        data = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data
        try:
            envelope = json.loads(data)
            scopes = envelope["scopes"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return
        recipients = set().union(*(self._scopes.get(scope, set()) for scope in scopes))
        for websocket in recipients:
            try:
                await websocket.send_text(data)
            except Exception:
                # браузер мог исчезнуть между предыдущим сообщением и этим
                await self.disconnect(websocket)

    @staticmethod
    def _is_valid_scope(scope: str) -> bool:
        """принять только короткие области доски или числового треда"""

        if scope.startswith("thread:"):
            return scope.removeprefix("thread:").isdigit()
        if scope.startswith("board:"):
            slug = scope.removeprefix("board:")
            return 1 <= len(slug) <= 63 and all(
                char.islower() or char.isdigit() or char in "_-" for char in slug
            )
        return False


class DisabledWebSocketManager(WebSocketManager):
    """безопасная заглушка для выключенного realtime"""

    def __init__(self) -> None:
        """создать заглушку без соединений и redis"""

    async def broadcast(self, event: dict[str, Any], scopes: Iterable[str] = ()) -> None:
        """не отправлять события, пока realtime выключен"""

    async def aclose(self) -> None:
        """не закрывать несуществующие соединения"""
