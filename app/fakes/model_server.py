import argparse
import asyncio
from collections import deque
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse


class FakeModel:
    def __init__(self, slots: int, token_seconds: float) -> None:
        self.slots = asyncio.Semaphore(slots)
        self.token_seconds = token_seconds
        self.processing = 0
        self.deferred = 0
        self.tokens = 0
        self.script: deque[int | float] = deque()


def create_app(slots: int = 2, token_seconds: float = 0.01) -> FastAPI:
    app = FastAPI(title="fake model server", docs_url=None, redoc_url=None)
    model = FakeModel(slots, token_seconds)
    app.state.model = model

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return (
            f"llamacpp:requests_processing {model.processing}\n"
            f"llamacpp:requests_deferred {model.deferred}\n"
            f"llamacpp:tokens_predicted_total {model.tokens}\n"
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        body: dict[str, Any] = await request.json()
        step = model.script.popleft() if model.script else None
        if isinstance(step, int):
            return JSONResponse({"error": "scripted failure"}, status_code=step)
        if isinstance(step, float):
            await asyncio.sleep(step)
        tokens = int(body.get("max_tokens", 16))
        model.deferred += 1
        async with model.slots:
            model.deferred -= 1
            model.processing += 1
            try:
                await asyncio.sleep(tokens * model.token_seconds)
            finally:
                model.processing -= 1
        model.tokens += tokens
        prompt = str(body["messages"][-1]["content"])
        return JSONResponse(
            {
                "model": body.get("model", "fake"),
                "choices": [{"message": {"role": "assistant", "content": "ok " * tokens}}],
                "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": tokens},
            }
        )

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run a fake model server for tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--token-seconds", type=float, default=0.01)
    args = parser.parse_args()
    uvicorn.run(create_app(args.slots, args.token_seconds), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
