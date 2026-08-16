import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.config import Settings
from app.queue.models import TERMINAL_STATES, JobState
from app.queue.redis_queue import RedisQueue
from app.workers.worker import Worker


def wait_until(check: Callable[[], bool], timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def state_of(queue: RedisQueue, job_id: str) -> JobState | None:
    job = queue.get(job_id, with_history=False)
    return job.state if job else None


def finished(queue: RedisQueue, *ids: str) -> Callable[[], bool]:
    return lambda: all(state_of(queue, i) in TERMINAL_STATES for i in ids)


@contextmanager
def running_worker(queue: RedisQueue, settings: Settings, **kwargs: object) -> Iterator[Worker]:
    worker = Worker(queue, settings, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        yield worker
    finally:
        worker.stop()
        thread.join(timeout=settings.shutdown_grace_seconds + 5)
        assert not thread.is_alive()


def test_worker_drives_jobs_to_the_right_end_state(queue: RedisQueue, settings: Settings) -> None:
    add = queue.enqueue("add", {"a": 2, "b": 3})
    flaky = queue.enqueue("flaky", {"fail_times": 1}, max_attempts=3)
    doomed = queue.enqueue("flaky", {"fail_times": 5}, max_attempts=2)
    bad = queue.enqueue("fail", {"message": "nope"})
    invalid = queue.enqueue("add", {"a": "x"})
    unknown = queue.enqueue("rm -rf", {})

    with running_worker(queue, settings, concurrency=2):
        assert wait_until(
            finished(queue, add.id, flaky.id, doomed.id, bad.id, invalid.id, unknown.id)
        )

    def get(job_id: str):  # type: ignore[no-untyped-def]
        job = queue.get(job_id)
        assert job is not None
        return job

    assert get(add.id).state is JobState.SUCCEEDED and get(add.id).result == 5
    assert get(flaky.id).state is JobState.SUCCEEDED and get(flaky.id).attempts == 2
    assert get(doomed.id).state is JobState.DEAD_LETTER and get(doomed.id).attempts == 2
    assert get(bad.id).state is JobState.FAILED and get(bad.id).error == "PermanentJobError: nope"
    assert get(invalid.id).state is JobState.FAILED and get(invalid.id).attempts == 1
    assert get(unknown.id).error == "Unknown job type: rm -rf"


def test_worker_heartbeat_is_visible_and_removed_on_stop(
    queue: RedisQueue, settings: Settings
) -> None:
    with running_worker(queue, settings, worker_id="w-visible"):
        assert wait_until(lambda: any(w["id"] == "w-visible" for w in queue.workers()))
        worker = next(w for w in queue.workers() if w["id"] == "w-visible")
        assert worker["alive"] and worker["status"] in {"idle", "busy"}
    assert queue.workers() == []


def test_idle_worker_stops_quickly(queue: RedisQueue, settings: Settings) -> None:
    start = time.monotonic()
    with running_worker(queue, settings):
        time.sleep(0.3)
    assert time.monotonic() - start < 3


def test_shutdown_waits_for_running_job(queue: RedisQueue, settings: Settings) -> None:
    job = queue.enqueue("sleep", {"seconds": 0.8})
    with running_worker(queue, settings):
        assert wait_until(lambda: state_of(queue, job.id) is JobState.RUNNING)
    assert state_of(queue, job.id) is JobState.SUCCEEDED


def test_shutdown_after_grace_releases_job(queue: RedisQueue, redis_client: object) -> None:
    settings = queue.s.model_copy(update={"shutdown_grace_seconds": 0.2})
    job = queue.enqueue("sleep", {"seconds": 5})
    with running_worker(queue, settings):
        assert wait_until(lambda: state_of(queue, job.id) is JobState.RUNNING)
    released = queue.get(job.id)
    assert released is not None
    assert released.state is JobState.QUEUED and released.attempts == 0
    assert released.history[-1]["event"] == "released"


def test_killed_worker_job_is_recovered_by_another(queue: RedisQueue, settings: Settings) -> None:
    lease = 1.0
    env = {
        **os.environ,
        "REDIS_URL": settings.redis_url,
        "KEY_PREFIX": settings.key_prefix,
        "LEASE_SECONDS": str(lease),
        "HEARTBEAT_SECONDS": "0.2",
        "LOG_LEVEL": "WARNING",
    }
    job = queue.enqueue("sleep", {"seconds": 3})
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.workers", "--id", "doomed"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_until(lambda: state_of(queue, job.id) is JobState.RUNNING, timeout=20)
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    rescuer_settings = settings.model_copy(update={"lease_seconds": 10.0})
    killed_at = time.monotonic()
    with running_worker(queue, rescuer_settings, worker_id="rescuer"):
        assert wait_until(finished(queue, job.id), timeout=15)

    done = queue.get(job.id)
    assert done is not None
    assert done.state is JobState.SUCCEEDED and done.attempts == 2
    assert done.worker_id == "rescuer"
    events = [h["event"] for h in done.history]
    assert events == ["submitted", "started", "recovered", "started", "succeeded"]
    assert time.monotonic() - killed_at < lease + 6
