"""websocket-команды и жизненный цикл соединения realtime"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Annotated

from asyncpg import PostgresError
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.dependencies import PostgresPoolDependency, WsManagerDependency
from app.core.config import Settings, get_settings
from app.core.security import SESSION_COOKIE, hash_token
from app.repositories.realtime import RealtimeRepository
from app.repositories.users import UsersRepository
from app.schemas.realtime import COMMAND, PongCommand
from app.services.realtime import subscribe
from app.ws.manager import MAX_SCOPES_PER_CONNECTION, Client, Close, WebSocketManager

router = APIRouter(tags=["ws"])

HEARTBEAT_SECONDS = 30
PONG_TIMEOUT = 10
SEND_TIMEOUT = 5
ACCESS_TIMEOUT = 3
MAX_COMMAND_BYTES = 2048
MAX_COMMANDS_PER_SECOND = 20


@dataclass(slots=True)
class Heartbeat:
    """ожидаемый ответ на последний ping"""

    nonce: str | None = None
    pong: asyncio.Event = field(default_factory=asyncio.Event)


@router.websocket("/ws")
async def ws_events(
    websocket: WebSocket,
    manager: WsManagerDependency,
    pool: PostgresPoolDependency,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """принимать соединения только с разрешённым origin и действующей cookie"""

    origins = websocket.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in settings.realtime_origins:
        await websocket.close(code=1008)
        return

    client = manager.connect()
    if client is None:
        await websocket.close(code=1013)
        return

    try:
        token = websocket.cookies.get(SESSION_COOKIE)
        if token is not None:
            try:
                user = await asyncio.wait_for(
                    UsersRepository(pool).get_user_by_session(hash_token(token)), ACCESS_TIMEOUT
                )
            except (PostgresError, OSError, TimeoutError):
                await websocket.close(code=1013)
                return
            if user is None:
                await websocket.close(code=1008)
                return
        if client.close is not None:
            await websocket.close(code=client.close.code)
            return
        await asyncio.wait_for(websocket.accept(), SEND_TIMEOUT)
        await _serve(websocket, manager, client, RealtimeRepository(pool))
    finally:
        manager.disconnect(client)


async def _serve(
    websocket: WebSocket, manager: WebSocketManager, client: Client, repository: RealtimeRepository
) -> None:
    """остановить чтение и запись перед отправкой кадра закрытия"""

    heartbeat = Heartbeat()
    tasks = (
        asyncio.create_task(_read(websocket, manager, client, repository, heartbeat)),
        asyncio.create_task(_write(websocket, client)),
        asyncio.create_task(_heartbeat(manager, client, heartbeat)),
        asyncio.create_task(client.stopped.wait()),
    )
    disconnected = False
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except WebSocketDisconnect:
                disconnected = True
            except TimeoutError:
                manager.close_client(client, Close(1013, "operation timed out"))
            except (OSError, PostgresError):
                manager.close_client(client, Close(1013, "operation unavailable"))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if not disconnected and client.close is not None:
        try:
            await asyncio.wait_for(
                websocket.close(code=client.close.code, reason=client.close.reason), SEND_TIMEOUT
            )
        except (TimeoutError, OSError, WebSocketDisconnect):
            return


async def _write(websocket: WebSocket, client: Client) -> None:
    """отправлять команды и события одним writer с ограничением времени"""

    while True:
        message = await client.messages.get()
        if client.close is not None:
            return
        await asyncio.wait_for(websocket.send_json(message), SEND_TIMEOUT)


async def _heartbeat(manager: WebSocketManager, client: Client, heartbeat: Heartbeat) -> None:
    """отключить клиента, если он не ответил на текущий ping"""

    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        heartbeat.pong.clear()
        heartbeat.nonce = token_hex(8)
        manager.enqueue(client, {"type": "ping", "nonce": heartbeat.nonce})
        try:
            await asyncio.wait_for(heartbeat.pong.wait(), PONG_TIMEOUT)
        except TimeoutError:
            manager.close_client(client, Close(1008, "heartbeat timeout"))
            return
        heartbeat.nonce = None


async def _read(
    websocket: WebSocket, manager: WebSocketManager, client: Client,
    repository: RealtimeRepository, heartbeat: Heartbeat,
) -> None:
    """читать только короткие команды с ограничением частоты"""

    arrivals: deque[float] = deque()
    loop = asyncio.get_running_loop()
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(frame["code"])
        now = loop.time()
        while arrivals and now - arrivals[0] >= 1:
            arrivals.popleft()
        if len(arrivals) >= MAX_COMMANDS_PER_SECOND:
            manager.close_client(client, Close(1008, "command rate exceeded"))
            return
        arrivals.append(now)
        raw = frame.get("text")
        if raw is None:
            manager.close_client(client, Close(1003, "text commands required"))
            return
        if len(raw) > MAX_COMMAND_BYTES or len(raw.encode("utf-8")) > MAX_COMMAND_BYTES:
            manager.close_client(client, Close(1009, "command too large"))
            return
        try:
            command = COMMAND.validate_json(raw)
        except ValidationError:
            manager.enqueue(client, {"type": "error", "detail": "invalid command"})
            continue

        if isinstance(command, PongCommand):
            if command.nonce == heartbeat.nonce:
                heartbeat.pong.set()
        elif command.type == "unsubscribe":
            client.subscriptions.pop(command.scope, None)
            manager.enqueue(client, {"type": "unsubscribed", "scope": command.scope})
        elif (
            command.scope in client.subscriptions
            or len(client.subscriptions) < MAX_SCOPES_PER_CONNECTION
        ) and await asyncio.wait_for(
            subscribe(manager, client, repository, command.scope), ACCESS_TIMEOUT
        ):
            manager.enqueue(client, {"type": "subscribed", "scope": command.scope})
        else:
            manager.enqueue(client, {"type": "error", "detail": "subscription rejected"})
