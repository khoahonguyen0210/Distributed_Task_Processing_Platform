import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router as jobs_router
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.queue.redis_queue import RedisQueue

log = logging.getLogger("taskq.api")


def create_app(settings: Settings | None = None, queue: RedisQueue | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_queue = queue is None
        app.state.settings = settings
        app.state.queue = queue or RedisQueue.from_settings(settings)
        try:
            yield
        finally:
            if own_queue:
                app.state.queue.close()

    app = FastAPI(
        title="taskq",
        version="1.0.0",
        summary="Submit jobs, follow their state, and watch workers process them.",
        lifespan=lifespan,
    )
    app.include_router(jobs_router)

    @app.exception_handler(redis.RedisError)
    async def redis_down(request: Request, exc: redis.RedisError) -> JSONResponse:
        log.error("redis error: %s", type(exc).__name__)
        return JSONResponse(
            {"detail": "Redis is not available. Try again later."},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        create_app(settings), host=settings.api_host, port=settings.api_port, log_config=None
    )


if __name__ == "__main__":
    run()
