from typing import Any

import redis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.routes import get_queue
from app.observability.metrics import collect, prometheus_text

router = APIRouter(tags=["health and metrics"])


@router.get("/health/live")
def live() -> dict[str, str]:
    """The API process is running. Says nothing about Redis or workers."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    """503 when Redis cannot be reached. 'degraded' means Redis is fine but no worker is alive."""
    queue = get_queue(request)
    try:
        queue.ping()
        alive = sum(1 for w in queue.workers() if w["alive"])
    except redis.RedisError:
        return JSONResponse({"status": "down", "redis": "down", "workers_alive": None}, 503)
    status = "ok" if alive else "degraded"
    return JSONResponse({"status": status, "redis": "ok", "workers_alive": alive})


@router.get("/api/workers")
def workers(request: Request) -> list[dict[str, Any]]:
    return get_queue(request).workers()


@router.get("/api/metrics")
def metrics(request: Request) -> dict[str, Any]:
    return collect(get_queue(request))


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics_prometheus(request: Request) -> PlainTextResponse:
    text = prometheus_text(collect(get_queue(request)))
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")
