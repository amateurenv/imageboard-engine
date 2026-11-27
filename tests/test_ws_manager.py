"""проверка безопасной основы realtime"""

import json

import pytest

from app.ws.manager import MAX_SCOPES_PER_CONNECTION, WebSocketManager


class FakeRedis:
    """запомнить событие вместо отправки в redis"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, channel: str, payload: str) -> None:
        """сохранить опубликованное событие"""

        assert channel == "ws:events"
        self.messages.append(payload)


class FakeWebSocket:
    """простое websocket-соединение для unit-теста"""

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.messages: list[str] = []

    async def accept(self) -> None:
        """пометить соединение принятым"""

        self.accepted = True

    async def close(self, code: int, reason: str) -> None:
        """пометить соединение закрытым"""

        self.closed = True

    async def send_text(self, data: str) -> None:
        """сохранить отправленное клиенту событие"""

        self.messages.append(data)


@pytest.mark.anyio
async def test_event_reaches_only_its_scope() -> None:
    """событие треда не уходит подписчику другой доски"""

    redis = FakeRedis()
    manager = WebSocketManager(redis)  # type: ignore[arg-type]
    thread_client = FakeWebSocket()
    board_client = FakeWebSocket()
    await manager.connect(thread_client)  # type: ignore[arg-type]
    await manager.connect(board_client)  # type: ignore[arg-type]
    assert await manager.subscribe(thread_client, "thread:15")  # type: ignore[arg-type]
    assert await manager.subscribe(board_client, "board:news")  # type: ignore[arg-type]

    await manager.broadcast({"event": "post_created"}, ["thread:15"])
    await manager._deliver(redis.messages[0])

    assert json.loads(thread_client.messages[0])["event"]["event"] == "post_created"
    assert board_client.messages == []


@pytest.mark.anyio
async def test_subscription_has_scope_limit() -> None:
    """клиент не может подписаться на бесконечное число тредов"""

    manager = WebSocketManager(FakeRedis())  # type: ignore[arg-type]
    client = FakeWebSocket()
    await manager.connect(client)  # type: ignore[arg-type]
    for thread_id in range(MAX_SCOPES_PER_CONNECTION):
        assert await manager.subscribe(client, f"thread:{thread_id}")  # type: ignore[arg-type]
    assert not await manager.subscribe(client, "thread:999")  # type: ignore[arg-type]
