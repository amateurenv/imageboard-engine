"""websocket-протокол realtime, маршрут пока не подключён к api"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.dependencies import WsManagerDependency
from app.ws.manager import HEARTBEAT_SECONDS

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def ws_events(websocket: WebSocket, ws_manager: WsManagerDependency) -> None:
    """держать подписки клиента на доски и треды"""

    if not await ws_manager.connect(websocket):
        return
    try:
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=HEARTBEAT_SECONDS
                )
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await _handle_message(websocket, ws_manager, raw_message)
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)


async def _handle_message(
    websocket: WebSocket, ws_manager: WsManagerDependency, raw_message: str
) -> None:
    """обработать короткую команду подписки или heartbeat"""

    try:
        message = json.loads(raw_message)
        action = message["type"]
    except (json.JSONDecodeError, KeyError, TypeError):
        await websocket.send_json({"type": "error", "detail": "invalid message"})
        return
    if action == "pong":
        return
    scope = message.get("scope")
    if not isinstance(scope, str):
        await websocket.send_json({"type": "error", "detail": "scope is required"})
        return
    if action == "subscribe":
        if await ws_manager.subscribe(websocket, scope):
            await websocket.send_json({"type": "subscribed", "scope": scope})
        else:
            await websocket.send_json({"type": "error", "detail": "subscription rejected"})
    elif action == "unsubscribe":
        await ws_manager.unsubscribe(websocket, scope)
        await websocket.send_json({"type": "unsubscribed", "scope": scope})
    else:
        await websocket.send_json({"type": "error", "detail": "unknown message type"})
