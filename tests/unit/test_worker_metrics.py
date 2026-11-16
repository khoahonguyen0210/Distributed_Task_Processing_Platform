import httpx

from app.core.config import Settings
from app.workers.metrics_server import serve_metrics
from app.workers.worker import Worker


class NoQueue:
    pass


def test_worker_metrics_are_served_over_http() -> None:
    settings = Settings.model_construct(shutdown_grace_seconds=1)
    worker = Worker(NoQueue(), settings, worker_id="w", concurrency=3)
    server = serve_metrics("127.0.0.1", 0, worker.render_metrics)
    try:
        port = server.server_address[1]
        text = httpx.get(f"http://127.0.0.1:{port}/metrics").text
        assert "taskq_worker_slots 3" in text
        assert "taskq_worker_active_jobs 0" in text
        assert "# TYPE taskq_worker_jobs_processed_total counter" in text
        assert httpx.get(f"http://127.0.0.1:{port}/other").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
