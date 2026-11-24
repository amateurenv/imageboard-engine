"""websocket-соединения и рассылка событий"""

import json
from typing import Any

from redis.asyncio import Redis
from starlette.websockets import WebSocket

# канал redis для рассылки событий между процессами fastapi
EVENTS_CHANNEL = "ws:events"


class WebSocketManager:
    """управление подключёнными websocket-клиентами"""

    def __init__(self, redis_client: Redis) -> None:
        self._connections: set[WebSocket] = set()
        self._redis_client = redis_client

    async def connect(self, websocket: WebSocket) -> None:
        """принять соединение"""

        await websocket.accept()
        self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """закрыть соединение"""

        self._connections.discard(websocket)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """публиковать событие в redis-канал, субскрайберы рассылают локально"""

        data = json.dumps(event, ensure_ascii=False)
        await self._redis_client.publish(EVENTS_CHANNEL, data)

    async def send_to_all(self, data: str) -> None:
        """разослать текст события всем подключённым"""

        for websocket in tuple(self._connections):
            try:
                await websocket.send_text(data)
            except Exception:
                # соединение могло умереть — убираем из списка
                self._connections.discard(websocket)

    async def consume_redis(self) -> None:
        """потреблять redis-канал и рассылать события локально (task в lifespan)"""

        async with self._redis_client.pubsub() as pubsub:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True)
                if message is None or message["type"] != "message":
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await self.send_to_all(data)
