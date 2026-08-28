import json
import time

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.queue.redis_queue import RedisQueue


def test_websocket_streams_job_state_changes(queue: RedisQueue, settings: Settings) -> None:
    app = create_app(settings, queue)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while not app.state.hub.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert app.state.hub.connected

        with client.websocket_connect("/ws/events") as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            job = queue.enqueue("add", {"a": 1, "b": 2})
            claimed = queue.claim("w1")
            assert claimed is not None
            queue.complete(claimed, 3, duration_ms=1)

            states = []
            while len(states) < 3:
                message = json.loads(ws.receive_text())
                if message["type"] == "job" and message["job_id"] == job.id:
                    states.append((message["previous"], message["state"]))
            assert states == [("", "queued"), ("queued", "running"), ("running", "succeeded")]


def test_reconnecting_client_can_reload_state_from_api(
    queue: RedisQueue, settings: Settings
) -> None:
    app = create_app(settings, queue)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_text()
        missed = queue.enqueue("add", {"a": 1, "b": 1})
        with client.websocket_connect("/ws/events") as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            jobs = client.get("/api/jobs").json()["jobs"]
            assert missed.id in {j["id"] for j in jobs}
        assert app.state.hub.subscribers == set()
