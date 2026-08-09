import json
import threading
import time

import pytest
import redis

from app.core.config import Settings
from app.queue.models import JobState, is_allowed
from app.queue.redis_queue import PayloadTooLargeError, RedisQueue, ResultTooLargeError


def force_expire_lease(queue: RedisQueue, job_id: str) -> None:
    queue.r.zadd(queue.key("leases"), {job_id: 0})


def make_due(queue: RedisQueue, job_id: str) -> None:
    queue.r.zadd(queue.key("scheduled"), {job_id: 0})


def test_enqueue_claim_complete(queue: RedisQueue) -> None:
    job = queue.enqueue("add", {"a": 1, "b": 2})
    assert job.state is JobState.QUEUED and job.attempts == 0

    claimed = queue.claim("w1")
    assert claimed is not None and claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempts == 1 and claimed.worker_id == "w1" and claimed.lease_token

    assert queue.complete(claimed, 3, duration_ms=5)
    done = queue.get(job.id)
    assert done is not None
    assert done.state is JobState.SUCCEEDED and done.result == 3
    assert done.lease_token is None
    assert [h["event"] for h in done.history] == ["submitted", "started", "succeeded"]
    assert queue.r.ttl(queue.key(f"job:{job.id}")) > 0
    assert queue.claim("w1") is None


def test_higher_priority_first_then_fifo(queue: RedisQueue) -> None:
    low = queue.enqueue("add", {"a": 0, "b": 0}, priority=1)
    first = queue.enqueue("add", {"a": 0, "b": 0}, priority=8)
    second = queue.enqueue("add", {"a": 0, "b": 0}, priority=8)
    mid = queue.enqueue("add", {"a": 0, "b": 0}, priority=5)

    order = [queue.claim("w") for _ in range(4)]
    assert [j.id for j in order if j] == [first.id, second.id, mid.id, low.id]


def test_competing_claims_never_share_a_job(queue: RedisQueue) -> None:
    ids = {queue.enqueue("add", {"a": i, "b": i}).id for i in range(60)}
    claimed: list[str] = []
    lock = threading.Lock()

    def grab(worker: str) -> None:
        client = redis.Redis.from_url(queue.s.redis_url, decode_responses=True)
        own = RedisQueue(client, queue.s)
        while (job := own.claim(worker)) is not None:
            with lock:
                claimed.append(job.id)
        client.close()

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


def test_delayed_job_is_not_claimed_early(queue: RedisQueue) -> None:
    job = queue.enqueue("add", {"a": 1, "b": 1}, delay_seconds=0.4)
    assert job.state is JobState.SCHEDULED
    assert queue.promote_due() == 0
    assert queue.claim("w") is None

    time.sleep(0.5)
    assert queue.promote_due() == 1
    claimed = queue.claim("w")
    assert claimed is not None and claimed.id == job.id
    assert claimed.started_at is not None and job.eligible_at is not None
    assert claimed.started_at >= job.eligible_at


def test_retryable_failure_retries_then_dead_letters(queue: RedisQueue) -> None:
    job = queue.enqueue("flaky", {"fail_times": 9}, max_attempts=2)

    first = queue.claim("w")
    assert first is not None
    assert queue.fail(first, "boom", retryable=True, retry_delay=60, duration_ms=1) == "retry"
    waiting = queue.get(job.id)
    assert waiting is not None and waiting.state is JobState.RETRY_SCHEDULED
    assert queue.claim("w") is None

    make_due(queue, job.id)
    assert queue.promote_due() == 1
    second = queue.claim("w")
    assert second is not None and second.attempts == 2
    assert queue.fail(second, "boom again", retryable=True, retry_delay=0, duration_ms=1) == "dead"

    dead = queue.get(job.id)
    assert dead is not None
    assert dead.state is JobState.DEAD_LETTER and dead.error == "boom again"
    assert [j.id for j in queue.dead_letters()] == [job.id]
    assert queue.stats()["dead_letter"] == 1


def test_permanent_failure_is_not_retried(queue: RedisQueue) -> None:
    job = queue.enqueue("fail", {"message": "bad input"})
    claimed = queue.claim("w")
    assert claimed is not None
    assert (
        queue.fail(claimed, "bad input", retryable=False, retry_delay=0, duration_ms=1) == "failed"
    )
    failed = queue.get(job.id)
    assert failed is not None and failed.state is JobState.FAILED and failed.attempts == 1


def test_error_text_is_truncated(queue: RedisQueue, settings: Settings) -> None:
    queue.enqueue("fail", {"message": "x"})
    claimed = queue.claim("w")
    assert claimed is not None
    queue.fail(claimed, "e" * 10_000, retryable=False, retry_delay=0, duration_ms=1)
    job = queue.get(claimed.id)
    assert job is not None and job.error is not None
    assert len(job.error) == settings.max_error_chars


def test_stale_lease_token_cannot_finish_job(queue: RedisQueue) -> None:
    queue.enqueue("add", {"a": 1, "b": 1})
    claimed = queue.claim("w1")
    assert claimed is not None
    force_expire_lease(queue, claimed.id)
    assert queue.recover_expired() == 1

    again = queue.claim("w2")
    assert again is not None and again.id == claimed.id and again.attempts == 2
    assert not queue.complete(claimed, 2, duration_ms=1)
    assert queue.fail(claimed, "late", retryable=True, retry_delay=0, duration_ms=1) is None
    assert not queue.renew(claimed)
    assert queue.renew(again)
    assert queue.complete(again, 2, duration_ms=1)


def test_expired_lease_at_last_attempt_goes_to_dead_letter(queue: RedisQueue) -> None:
    job = queue.enqueue("sleep", {"seconds": 1}, max_attempts=1)
    claimed = queue.claim("w1")
    assert claimed is not None
    force_expire_lease(queue, job.id)
    assert queue.recover_expired() == 1
    dead = queue.get(job.id)
    assert dead is not None and dead.state is JobState.DEAD_LETTER
    assert dead.error is not None and "Lease expired" in dead.error


def test_renewed_lease_is_not_recovered(queue: RedisQueue) -> None:
    queue.enqueue("add", {"a": 1, "b": 1})
    claimed = queue.claim("w1")
    assert claimed is not None
    assert queue.renew(claimed)
    assert queue.recover_expired() == 0


def test_cancel_only_waiting_jobs(queue: RedisQueue) -> None:
    waiting = queue.enqueue("add", {"a": 1, "b": 1}, delay_seconds=60)
    assert queue.cancel(waiting.id) == "cancelled"
    assert queue.r.zscore(queue.key("scheduled"), waiting.id) is None

    queue.enqueue("add", {"a": 1, "b": 1})
    running = queue.claim("w")
    assert running is not None
    assert queue.cancel(running.id) == "running"
    assert queue.cancel("0" * 32) is None


def test_replay_keeps_history_and_resets_attempts(queue: RedisQueue) -> None:
    job = queue.enqueue("flaky", {"fail_times": 5}, max_attempts=1)
    claimed = queue.claim("w")
    assert claimed is not None
    queue.fail(claimed, "boom", retryable=True, retry_delay=0, duration_ms=1)

    assert queue.replay(job.id)
    replayed = queue.get(job.id)
    assert replayed is not None
    assert replayed.state is JobState.QUEUED and replayed.attempts == 0
    assert replayed.error == "boom"
    assert [h["event"] for h in replayed.history][-2:] == ["dead_letter", "replayed"]
    assert queue.r.ttl(queue.key(f"job:{job.id}")) == -1
    assert not queue.replay(job.id)


def test_size_limits(queue: RedisQueue, settings: Settings) -> None:
    with pytest.raises(PayloadTooLargeError):
        queue.enqueue("add", {"blob": "x" * settings.max_payload_bytes})
    queue.enqueue("add", {"a": 1, "b": 1})
    claimed = queue.claim("w")
    assert claimed is not None
    with pytest.raises(ResultTooLargeError):
        queue.complete(claimed, "x" * settings.max_result_bytes, duration_ms=1)


def test_list_jobs_filters_and_skips_expired(queue: RedisQueue) -> None:
    a = queue.enqueue("add", {"a": 1, "b": 1})
    b = queue.enqueue("sleep", {"seconds": 0})
    time.sleep(0.002)  # keep c newer than b
    c = queue.enqueue("add", {"a": 2, "b": 2}, delay_seconds=30)
    queue.r.delete(queue.key(f"job:{a.id}"))

    assert [j.id for j in queue.list_jobs()] == [c.id, b.id]
    assert [j.id for j in queue.list_jobs(job_type="add")] == [c.id]
    assert [j.id for j in queue.list_jobs(state=JobState.QUEUED)] == [b.id]
    assert [j.id for j in queue.list_jobs(limit=1, offset=1)] == [b.id]
    assert queue.r.zscore(queue.key("jobs"), a.id) is None


def test_every_event_is_an_allowed_transition(queue: RedisQueue) -> None:
    pubsub = queue.r.pubsub()
    pubsub.subscribe(queue.key("events"))
    pubsub.get_message(timeout=1)

    ok = queue.enqueue("add", {"a": 1, "b": 1})
    job = queue.claim("w")
    assert job is not None
    queue.complete(job, 2, duration_ms=1)
    queue.enqueue("flaky", {"fail_times": 3}, max_attempts=2)
    for _ in range(2):
        job = queue.claim("w")
        assert job is not None
        queue.fail(job, "boom", retryable=True, retry_delay=0, duration_ms=1)
        queue.promote_due()
    queue.replay(job.id)
    queue.cancel(job.id)
    delayed = queue.enqueue("add", {"a": 1, "b": 1}, delay_seconds=30)
    queue.cancel(delayed.id)

    events = []
    while (msg := pubsub.get_message(timeout=0.2)) is not None:
        events.append(json.loads(msg["data"]))
    pubsub.close()

    assert len(events) >= 12
    for event in events:
        previous = JobState(event["previous"]) if event["previous"] else None
        assert is_allowed(previous, JobState(event["state"])), event
    assert {e["job_id"] for e in events} >= {ok.id, delayed.id}


def test_stats_counts(queue: RedisQueue) -> None:
    queue.enqueue("add", {"a": 1, "b": 1})
    queue.enqueue("add", {"a": 1, "b": 1}, delay_seconds=30)
    queue.enqueue("add", {"a": 1, "b": 1})
    queue.claim("w")
    stats = queue.stats()
    assert stats["total_submitted"] == 3
    assert (stats["ready"], stats["scheduled"], stats["running"]) == (1, 1, 1)
