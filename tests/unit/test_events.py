import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.events.hub import RESYNC, EventHub, Subscriber
from app.main import create_app
from tests.conftest import make_settings


def test_slow_subscriber_gets_resync_instead_of_old_events() -> None:
    async def scenario() -> list[str]:
        sub = Subscriber(size=3)
        for i in range(5):
            sub.offer(f"event-{i}")
        return [await sub.get() for _ in range(sub.queue.qsize())]

    assert asyncio.run(scenario()) == [RESYNC, "event-4"]


def test_hub_fans_out_to_every_subscriber() -> None:
    async def scenario() -> tuple[str, str]:
        hub = EventHub("redis://unused", "events")
        a, b = hub.subscribe(), hub.subscribe()
        hub.publish_local("x")
        hub.unsubscribe(b)
        hub.publish_local("y")
        return await a.get() + await a.get(), await b.get()

    assert asyncio.run(scenario()) == ("xy", "x")


def test_websocket_rejects_other_origins() -> None:
    app = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    bad_origin = {"origin": "https://evil.example"}
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws/events", headers=bad_origin),
    ):
        pass
    assert exc.value.code == 1008


def test_websocket_says_hello_when_redis_is_down() -> None:
    app = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    origin = {"origin": "http://testserver"}
    with TestClient(app) as client, client.websocket_connect("/ws/events", headers=origin) as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello", "redis": False}
