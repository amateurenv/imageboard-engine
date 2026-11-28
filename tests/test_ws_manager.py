"""проверка изоляции областей и ограничений рассылки"""

import asyncio

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from redis.asyncio import Redis

import app.api.v1.ws as websocket_api
from app.core.config import get_settings
from app.schemas.realtime import RealtimeEvent
from app.services.realtime import subscribe
from app.ws.manager import FEED_UNAVAILABLE, Client, Subscription, WebSocketManager


def connect(manager: WebSocketManager) -> Client:
    """взять свободное соединение"""

    client = manager.connect()
    assert client is not None
    return client


def test_events_only_expose_the_subscribed_scope() -> None:
    """доска получает только свою область, тред теряет подписку при скрытии"""

    manager = WebSocketManager(Redis(), "test")
    assert manager.connect() is None
    manager.ready.set()
    reader = connect(manager)
    other = connect(manager)
    reader.subscriptions = {"board:b": Subscription(1, None), "thread:15": Subscription(1, 15)}
    other.subscriptions = {"thread:16": Subscription(1, 16), "board:c": Subscription(2, None)}

    manager.deliver(RealtimeEvent(board_id=1, thread_id=15, unavailable=True))

    assert reader.messages.get_nowait() == {"type": "changed", "scope": "board:b"}
    assert reader.messages.get_nowait() == {"type": "unavailable", "scope": "thread:15"}
    assert set(reader.subscriptions) == {"board:b"}
    assert other.messages.empty()

    manager.deliver(RealtimeEvent(board_id=1, thread_id=None, unavailable=True))
    assert other.messages.get_nowait() == {"type": "unavailable", "scope": "thread:16"}
    assert set(other.subscriptions) == {"board:c"}


def test_slow_client_and_redis_gap_release_scopes() -> None:
    """переполнение очереди не тормозит соседей, пропуск требует переподключения"""

    manager = WebSocketManager(Redis(), "test", max_connections=2)
    manager.ready.set()
    slow = connect(manager)
    fast = connect(manager)
    assert manager.connect() is None
    fast.subscriptions["board:b"] = Subscription(1, None)
    for _ in range(slow.messages.maxsize + 1):
        manager.enqueue(slow, {"type": "ping"})

    manager.deliver(RealtimeEvent(board_id=1, thread_id=1, unavailable=False))
    assert slow.close is not None and slow.close.reason == "slow client"
    assert fast.messages.get_nowait() == {"type": "changed", "scope": "board:b"}
    manager._feed_lost()
    assert fast.close == FEED_UNAVAILABLE
    assert fast.subscriptions == {}
    assert manager.connect() is None


@pytest.mark.anyio
async def test_revocation_during_authorization() -> None:
    """отзыв во время чтения postgresql исключает позднюю запись подписки"""

    entered = asyncio.Event()
    resume = asyncio.Event()

    class Repository:
        async def get_thread_board_id(self, thread_id: int) -> int:
            entered.set()
            await resume.wait()
            return 1

    manager = WebSocketManager(Redis(), "test")
    manager.ready.set()
    client = connect(manager)
    task = asyncio.create_task(subscribe(manager, client, Repository(), "thread:15"))
    await entered.wait()
    manager.deliver(RealtimeEvent(board_id=1, thread_id=15, unavailable=True))
    resume.set()

    assert not await task
    assert not client.subscriptions


def test_websocket_heartbeat_and_command_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """проверить nonce, закрытие молчащего клиента и пределы команд"""

    application = FastAPI()
    application.include_router(websocket_api.router)
    manager = WebSocketManager(Redis(), "test")
    manager.ready.set()
    application.state.ws_manager = manager
    application.state.postgres_pool = object()
    origin = "http://testserver"
    settings = get_settings().model_copy(update={"realtime_allowed_origins": origin})
    application.dependency_overrides[get_settings] = lambda: settings

    class Repository:
        async def get_board_id(self, slug: str) -> int:
            return ord(slug)

    monkeypatch.setattr(websocket_api, "RealtimeRepository", lambda pool: Repository())
    monkeypatch.setattr(websocket_api, "MAX_SCOPES_PER_CONNECTION", 2)

    with TestClient(application) as client:
        with monkeypatch.context() as heartbeat:
            heartbeat.setattr(websocket_api, "HEARTBEAT_SECONDS", 0.01)
            heartbeat.setattr(websocket_api, "PONG_TIMEOUT", 0.3)
            with client.websocket_connect("/ws", headers={"Origin": origin}) as ws:
                ping = ws.receive_json()
                assert ping["type"] == "ping"
                ws.send_json({"type": "pong", "nonce": ping["nonce"]})
                next_ping = ws.receive_json()
                assert next_ping["type"] == "ping" and next_ping["nonce"] != ping["nonce"]
                ws.send_json({"type": "pong", "nonce": ping["nonce"]})
                with pytest.raises(WebSocketDisconnect) as timeout:
                    ws.receive_json()
                assert (timeout.value.code, timeout.value.reason) == (1008, "heartbeat timeout")

        with client.websocket_connect("/ws", headers={"Origin": origin}) as ws:
            ws.send_text(" " * (websocket_api.MAX_COMMAND_BYTES + 1))
            with pytest.raises(WebSocketDisconnect) as too_large:
                ws.receive_json()
            assert too_large.value.code == 1009

        with client.websocket_connect("/ws", headers={"Origin": origin}) as ws:
            for scope in ("board:b", "board:c", "board:b"):
                ws.send_json({"type": "subscribe", "scope": scope})
                assert ws.receive_json() == {"type": "subscribed", "scope": scope}
            ws.send_json({"type": "subscribe", "scope": "board:d"})
            assert ws.receive_json() == {"type": "error", "detail": "subscription rejected"}
            ws.send_json({"type": "unsubscribe", "scope": "board:b"})
            assert ws.receive_json() == {"type": "unsubscribed", "scope": "board:b"}
            ws.send_json({"type": "subscribe", "scope": "board:d"})
            assert ws.receive_json() == {"type": "subscribed", "scope": "board:d"}

        with client.websocket_connect("/ws", headers={"Origin": origin}) as ws:
            for _ in range(websocket_api.MAX_COMMANDS_PER_SECOND):
                ws.send_json({"type": "unsubscribe", "scope": "board:b"})
                assert ws.receive_json() == {"type": "unsubscribed", "scope": "board:b"}
            ws.send_json({"type": "unsubscribe", "scope": "board:b"})
            with pytest.raises(WebSocketDisconnect) as rate:
                ws.receive_json()
            assert (rate.value.code, rate.value.reason) == (1008, "command rate exceeded")
