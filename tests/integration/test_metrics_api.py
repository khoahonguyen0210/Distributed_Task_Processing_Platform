from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.queue.redis_queue import RedisQueue


def test_ready_reports_degraded_without_workers(queue: RedisQueue, settings: Settings) -> None:
    with TestClient(create_app(settings, queue)) as client:
        assert client.get("/health/ready").json() == {
            "status": "degraded",
            "redis": "ok",
            "workers_alive": 0,
        }
        queue.worker_heartbeat("w1", {"status": "idle"})
        assert client.get("/health/ready").json()["status"] == "ok"
        workers = client.get("/api/workers").json()
        assert [w["id"] for w in workers] == ["w1"] and workers[0]["alive"]


def test_metrics_reflect_queue(queue: RedisQueue, settings: Settings) -> None:
    for _ in range(3):
        queue.enqueue("add", {"a": 1, "b": 1})
    job = queue.claim("w")
    assert job is not None
    queue.complete(job, 2, duration_ms=40)

    with TestClient(create_app(settings, queue)) as client:
        m = client.get("/api/metrics").json()
        assert m["queue"] == {"ready": 2, "scheduled": 0, "running": 0, "dead_letter": 0}
        assert m["totals"]["submitted"] == 3 and m["totals"]["succeeded"] == 1
        assert m["job_duration_ms"]["p50"] == 40 and m["job_duration_ms"]["samples"] == 1

        text = client.get("/metrics").text
        assert 'taskq_queue_jobs{queue="ready"} 2' in text
        assert 'taskq_job_duration_ms{quantile="0.95"} 40.0' in text
