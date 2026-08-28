import asyncio
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events.hub import EventHub

router = APIRouter()


def same_origin(websocket: WebSocket) -> bool:
    """Blocks other websites from reading the stream through a visitor's browser."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == websocket.headers.get("host")


async def wait_for_close(websocket: WebSocket) -> None:
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    if not same_origin(websocket):
        await websocket.close(code=1008)
        return
    hub: EventHub = websocket.app.state.hub
    await websocket.accept()
    sub = hub.subscribe()
    closed = asyncio.create_task(wait_for_close(websocket))
    try:
        await websocket.send_text(json.dumps({"type": "hello", "redis": hub.connected}))
        while True:
            next_message = asyncio.create_task(sub.get())
            done, _ = await asyncio.wait(
                {next_message, closed}, return_when=asyncio.FIRST_COMPLETED
            )
            if closed in done:
                next_message.cancel()
                break
            await websocket.send_text(next_message.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(sub)
        closed.cancel()
