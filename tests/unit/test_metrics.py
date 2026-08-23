from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.metrics import percentile, prometheus_text
from tests.conftest import make_settings


def test_percentile_nearest_rank() -> None:
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 100) == 100
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None


def test_prometheus_text_skips_missing_values() -> None:
    text = prometheus_text(
        {
            "queue": {"ready": 2},
            "totals": {"succeeded": 5},
            "workers": {"alive": 1, "stale": 0},
            "job_duration_ms": {"samples": 0, "p50": None, "p95": None},
        }
    )
    assert 'taskq_queue_jobs{queue="ready"} 2' in text
    assert 'taskq_jobs_total{event="succeeded"} 5' in text
    assert (
        "quantile" not in text.split("# TYPE taskq_job_duration_ms summary")[1].split("_count")[0]
    )


def test_health_without_redis() -> None:
    with TestClient(create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 503 and response.json()["redis"] == "down"
