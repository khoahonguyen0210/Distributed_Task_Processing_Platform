import threading
import time
from collections.abc import Iterator

import pytest
import redis
import uvicorn
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.fakes.model_server import FakeModel, create_app
from app.main import create_app as create_api
from app.queue.models import JobState
from app.queue.redis_queue import RedisQueue
from tests.conftest import clear_prefix, make_settings
from tests.integration.test_worker import finished, running_worker, wait_until

PROMPT = {"prompt": "Say ok.", "max_tokens": 4}


@pytest.fixture
def fake_model() -> Iterator[tuple[str, FakeModel]]:
    app = create_app(slots=2, token_seconds=0.001)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_until(lambda: server.started, timeout=10)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", app.state.model
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def llm_settings(
    redis_client: redis.Redis, fake_model: tuple[str, FakeModel]
) -> Iterator[Settings]:
    s = make_settings(llm_server_url=fake_model[0], llm_read_timeout_seconds=0.5)
    yield s
    clear_prefix(redis_client, s.key_prefix)


@pytest.fixture
def llm_queue(redis_client: redis.Redis, llm_settings: Settings) -> RedisQueue:
    return RedisQueue(redis_client, llm_settings)


def run_jobs(queue: RedisQueue, settings: Settings, *ids: str) -> None:
    with running_worker(queue, settings, concurrency=2):
        assert wait_until(finished(queue, *ids), timeout=20)


def test_llm_job_returns_text_and_token_counts(
    llm_queue: RedisQueue, llm_settings: Settings
) -> None:
    job = llm_queue.enqueue("llm_generate", PROMPT)
    run_jobs(llm_queue, llm_settings, job.id)
    done = llm_queue.get(job.id)
    assert done is not None and done.state is JobState.SUCCEEDED and done.attempts == 1
    assert done.result["text"] == "ok ok ok ok " and done.result["completion_tokens"] == 4
    assert done.result["model"] == "qwen2.5-0.5b-instruct" and not done.result["truncated"]


@pytest.mark.parametrize(
    ("script", "state", "attempts", "error"),
    [
        ([503], JobState.SUCCEEDED, 2, None),
        ([1.5], JobState.SUCCEEDED, 2, None),
        ([400], JobState.FAILED, 1, "refused the request (400)"),
        ([503, 503], JobState.DEAD_LETTER, 2, "answered 503"),
    ],
)
def test_model_server_errors_map_to_retries(
    llm_queue: RedisQueue,
    llm_settings: Settings,
    fake_model: tuple[str, FakeModel],
    script: list[int | float],
    state: JobState,
    attempts: int,
    error: str | None,
) -> None:
    fake_model[1].script.extend(script)
    job = llm_queue.enqueue("llm_generate", PROMPT, max_attempts=2)
    run_jobs(llm_queue, llm_settings, job.id)
    done = llm_queue.get(job.id)
    assert done is not None and done.state is state and done.attempts == attempts
    if error:
        assert done.error is not None and error in done.error


def test_timeout_counts_as_a_retryable_error(llm_queue: RedisQueue, llm_settings: Settings) -> None:
    job = llm_queue.enqueue("llm_generate", PROMPT, max_attempts=1)
    llm_settings_slow = llm_settings.model_copy(update={"llm_read_timeout_seconds": 0.001})
    with running_worker(llm_queue, llm_settings_slow):
        assert wait_until(finished(llm_queue, job.id), timeout=20)
    done = llm_queue.get(job.id)
    assert done is not None and done.state is JobState.DEAD_LETTER
    assert done.error is not None and "did not answer in time" in done.error


def test_unreachable_model_server_is_retried(redis_client: redis.Redis) -> None:
    settings = make_settings(llm_server_url="http://127.0.0.1:1")
    queue = RedisQueue(redis_client, settings)
    try:
        job = queue.enqueue("llm_generate", PROMPT, max_attempts=2)
        run_jobs(queue, settings, job.id)
        done = queue.get(job.id)
        assert done is not None and done.state is JobState.DEAD_LETTER and done.attempts == 2
        assert done.error is not None and "cannot reach the model server" in done.error
    finally:
        clear_prefix(redis_client, settings.key_prefix)


def test_expired_jobs_never_reach_the_model_server(
    llm_queue: RedisQueue, llm_settings: Settings, fake_model: tuple[str, FakeModel]
) -> None:
    jobs = [llm_queue.enqueue("llm_generate", PROMPT, deadline_seconds=0.05) for _ in range(3)]
    time.sleep(0.1)
    run_jobs(llm_queue, llm_settings, *(j.id for j in jobs))
    done = [llm_queue.get(j.id) for j in jobs]
    assert {j.state for j in done if j} == {JobState.EXPIRED} and all(done)
    assert fake_model[1].tokens == 0


def test_api_checks_llm_payloads_and_sets_a_deadline(llm_queue: RedisQueue) -> None:
    with TestClient(create_api(llm_queue.s, llm_queue)) as client:
        too_long = {"prompt": "x" * 2001}
        for payload in (too_long, {"prompt": "hi", "model": "other"}, {"prompt": ""}):
            response = client.post("/api/jobs", json={"type": "llm_generate", "payload": payload})
            assert response.status_code == 422, payload
        job = client.post("/api/jobs", json={"type": "llm_generate", "payload": PROMPT}).json()
        assert job["deadline_at"] - job["created_at"] == 300_000
        plain = client.post("/api/jobs", json={"type": "add", "payload": {"a": 1, "b": 1}}).json()
        assert plain["deadline_at"] is None
