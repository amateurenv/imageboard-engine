"""websocket-маршрут для realtime-событий"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.dependencies import WsManagerDependency

router = APIRouter(
    tags=["ws"],
)


@router.websocket("/ws")
async def ws_events(
    websocket: WebSocket,
    ws_manager: WsManagerDependency,
) -> None:
    """подписка на события сайта (post_created, thread_bumped)"""

    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
