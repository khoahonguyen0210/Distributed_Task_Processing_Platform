from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.queue.redis_queue import RedisQueue


@pytest.fixture
def client(queue: RedisQueue, settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, queue)) as c:
        yield c


def submit(client: TestClient, **body: object) -> dict[str, object]:
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202, response.text
    data: dict[str, object] = response.json()
    return data


def test_submit_and_read_job(client: TestClient) -> None:
    job = submit(client, type="add", payload={"a": 1, "b": 2}, priority=7)
    assert job["state"] == "queued" and job["priority"] == 7 and job["attempts"] == 0
    assert "lease_token" not in job

    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["id"] == job["id"]
    assert detail["history"][0]["event"] == "submitted"


def test_delayed_job_is_scheduled(client: TestClient) -> None:
    job = submit(client, type="sleep", payload={"seconds": 0}, delay_seconds=30)
    assert job["state"] == "scheduled"
    assert isinstance(job["eligible_at"], int) and isinstance(job["created_at"], int)
    assert job["eligible_at"] - job["created_at"] >= 29_000


@pytest.mark.parametrize(
    "body",
    [
        {"type": "shell", "payload": {"cmd": "ls"}},
        {"type": "add", "payload": {"a": 1}},
        {"type": "add", "payload": {"a": 1, "b": 2, "code": "x"}},
        {"type": "add", "payload": {"a": 1, "b": 2}, "priority": 10},
        {"type": "add", "payload": {"a": 1, "b": 2}, "delay_seconds": -1},
        {"type": "add", "payload": {"a": 1, "b": 2}, "extra": True},
        {"payload": {}},
    ],
)
def test_invalid_submissions_are_rejected(client: TestClient, body: dict[str, object]) -> None:
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) or all("input" not in error for error in detail)


def test_payload_size_limit(client: TestClient, settings: Settings) -> None:
    big = "x" * settings.max_payload_bytes
    response = client.post("/api/jobs", json={"type": "fail", "payload": {"message": big}})
    assert response.status_code == 422
    response = client.post(
        "/api/jobs", json={"type": "sleep", "payload": {"seconds": 1, "pad": big}}
    )
    assert response.status_code == 422


def test_unknown_and_malformed_ids(client: TestClient) -> None:
    assert client.get(f"/api/jobs/{'0' * 32}").status_code == 404
    assert client.get("/api/jobs/not-an-id").status_code == 422
    assert client.post(f"/api/jobs/{'0' * 32}/cancel").status_code == 404


def test_list_and_filter(client: TestClient) -> None:
    a = submit(client, type="add", payload={"a": 1, "b": 1})
    b = submit(client, type="sleep", payload={"seconds": 0}, delay_seconds=60)
    listed = client.get("/api/jobs").json()
    assert [j["id"] for j in listed["jobs"]] == [b["id"], a["id"]]
    only = client.get("/api/jobs", params={"state": "scheduled"}).json()["jobs"]
    assert [j["id"] for j in only] == [b["id"]]
    assert client.get("/api/jobs", params={"state": "bogus"}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 1000}).status_code == 422


def test_cancel(client: TestClient, queue: RedisQueue) -> None:
    job = submit(client, type="add", payload={"a": 1, "b": 1})
    response = client.post(f"/api/jobs/{job['id']}/cancel")
    assert response.status_code == 200 and response.json()["state"] == "cancelled"
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409

    running = submit(client, type="add", payload={"a": 1, "b": 1})
    queue.claim("w")
    assert client.post(f"/api/jobs/{running['id']}/cancel").status_code == 409


def test_dead_letters_and_replay(client: TestClient, queue: RedisQueue) -> None:
    job = submit(client, type="flaky", payload={"fail_times": 3}, max_attempts=1)
    claimed = queue.claim("w")
    assert claimed is not None
    queue.fail(claimed, "boom", retryable=True, retry_delay=0, duration_ms=1)

    dead = client.get("/api/dead-letters").json()["jobs"]
    assert [j["id"] for j in dead] == [job["id"]]
    response = client.post(f"/api/dead-letters/{job['id']}/replay")
    assert response.status_code == 200 and response.json()["state"] == "queued"
    assert client.post(f"/api/dead-letters/{job['id']}/replay").status_code == 409
    assert client.post(f"/api/dead-letters/{'0' * 32}/replay").status_code == 404


def test_job_types_list_schemas(client: TestClient) -> None:
    types = {t["name"]: t for t in client.get("/api/job-types").json()}
    assert set(types) == {"add", "sleep", "cpu", "flaky", "fail"}
    assert types["sleep"]["payload_schema"]["properties"]["seconds"]["maximum"] == 30


def test_openapi_is_served(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/jobs" in paths and "/api/dead-letters/{job_id}/replay" in paths
