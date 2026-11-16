import time

import pytest
import redis

from app.core.config import Settings
from app.queue.models import JobState
from app.queue.redis_queue import QueueFullError, RedisQueue
from tests.conftest import clear_prefix, make_settings


def test_job_past_its_deadline_expires_instead_of_running(queue: RedisQueue) -> None:
    late = queue.enqueue("add", {"a": 1, "b": 1}, deadline_seconds=0.05)
    on_time = queue.enqueue("add", {"a": 1, "b": 1}, deadline_seconds=60)
    assert late.deadline_at is not None and late.deadline_at - late.created_at == 50
    time.sleep(0.1)

    claimed = queue.claim("w")
    assert claimed is not None and claimed.id == on_time.id
    expired = queue.get(late.id)
    assert expired is not None and expired.state is JobState.EXPIRED
    assert expired.attempts == 0 and expired.finished_at is not None
    assert [h["event"] for h in expired.history] == ["submitted", "expired"]
    assert queue.r.ttl(queue.key(f"job:{late.id}")) > 0
    assert queue.stats()["total_expired"] == 1


def test_retry_after_the_deadline_expires(queue: RedisQueue) -> None:
    job = queue.enqueue("flaky", {"fail_times": 1}, deadline_seconds=0.2)
    claimed = queue.claim("w")
    assert claimed is not None
    queue.fail(claimed, "boom", retryable=True, retry_delay=0.3, duration_ms=1)
    queue.r.zadd(queue.key("scheduled"), {job.id: 0})
    time.sleep(0.25)
    queue.promote_due()
    assert queue.claim("w") is None
    done = queue.get(job.id)
    assert done is not None and done.state is JobState.EXPIRED and done.attempts == 1


def test_deadline_must_come_after_the_delay(queue: RedisQueue) -> None:
    with pytest.raises(ValueError):
        queue.enqueue("add", {"a": 1, "b": 1}, delay_seconds=10, deadline_seconds=5)


def test_full_queue_rejects_new_jobs(redis_client: redis.Redis) -> None:
    settings: Settings = make_settings(max_queue_depth=2)
    queue = RedisQueue(redis_client, settings)
    try:
        queue.enqueue("add", {"a": 1, "b": 1})
        queue.enqueue("add", {"a": 1, "b": 1})
        with pytest.raises(QueueFullError):
            queue.enqueue("add", {"a": 1, "b": 1})
        stats = queue.stats()
        assert stats["ready"] == 2 and stats["total_rejected"] == 1
        assert stats["total_submitted"] == 2

        assert queue.claim("w") is not None
        queue.enqueue("add", {"a": 1, "b": 1})
    finally:
        clear_prefix(redis_client, settings.key_prefix)


def test_success_adds_to_the_latency_histogram(queue: RedisQueue) -> None:
    for _ in range(2):
        queue.enqueue("add", {"a": 1, "b": 1})
        job = queue.claim("w")
        assert job is not None
        queue.complete(job, 2, duration_ms=1)
    latency = queue.latency()["add"]
    assert latency["count"] == 2 and latency["100"] == 2
    assert 0 <= latency["sum"] < 200
