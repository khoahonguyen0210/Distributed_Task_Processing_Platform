import contextlib
import logging
import os
import secrets
import signal
import threading
import time
from types import FrameType
from typing import Any

import redis
from pydantic import ValidationError

from app.core.config import Settings
from app.jobs import JobContext, PermanentJobError, UnknownJobTypeError, get_spec
from app.queue.backoff import retry_delay
from app.queue.models import Job
from app.queue.redis_queue import RedisQueue, ResultTooLargeError

log = logging.getLogger("taskq.worker")


def describe_error(exc: BaseException) -> str:
    """A short message for clients. Payload values and stack traces are left out."""
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(map(str, e['loc'])) or 'payload'}: {e['msg']}" for e in exc.errors()]
        return "Invalid payload: " + "; ".join(parts)
    if isinstance(exc, UnknownJobTypeError):
        return f"Unknown job type: {exc.args[0]}"
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class Worker:
    """One OS process. Each of its `concurrency` threads runs one job at a time."""

    def __init__(
        self,
        queue: RedisQueue,
        settings: Settings,
        worker_id: str | None = None,
        concurrency: int = 1,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.q = queue
        self.s = settings
        self.id = worker_id or f"worker-{secrets.token_hex(4)}"
        self.concurrency = concurrency
        self.started_at = int(time.time() * 1000)
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[str, Job] = {}
        self.processed = 0
        self.failures = 0

    # Lifecycle

    def stop(self) -> None:
        self.stopping.set()

    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._install_signal_handlers()
        log.info("worker started", extra={"event": "worker_started", "worker_id": self.id})
        self._heartbeat()

        house = threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True)
        house.start()
        # Daemon threads, so a job stuck past the grace period cannot keep the process alive.
        slots = [
            threading.Thread(target=self._slot_loop, name=f"slot-{n}", daemon=True)
            for n in range(self.concurrency)
        ]
        for t in slots:
            t.start()

        while not self.stopping.wait(0.5):
            pass

        log.info("worker stopping", extra={"event": "worker_stopping", "worker_id": self.id})
        deadline = time.monotonic() + self.s.shutdown_grace_seconds
        for t in slots:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        self._release_unfinished()
        house.join(timeout=self.s.maintenance_seconds + 2)
        with contextlib.suppress(redis.RedisError):
            self.q.remove_worker(self.id)
        log.info("worker stopped", extra={"event": "worker_stopped", "worker_id": self.id})

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, frame: FrameType | None) -> None:
            if self.stopping.is_set():
                log.warning("second stop signal, exiting now", extra={"worker_id": self.id})
                self._release_unfinished()
                os._exit(1)
            self.stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), handle)

    def _release_unfinished(self) -> None:
        with self._lock:
            unfinished = list(self._active.values())
            self._active.clear()
        for job in unfinished:
            try:
                if self.q.release(job):
                    log.warning(
                        "job released",
                        extra={"event": "released", "job_id": job.id, "worker_id": self.id},
                    )
            except redis.RedisError:
                log.exception(
                    "could not release job; its lease will expire", extra={"job_id": job.id}
                )

    # Background work: heartbeats, lease renewal, and queue maintenance

    def _heartbeat(self) -> None:
        with self._lock:
            active = list(self._active.values())
        self.q.worker_heartbeat(
            self.id,
            {
                "pid": os.getpid(),
                "started_at": self.started_at,
                "concurrency": self.concurrency,
                "status": "stopping" if self.stopping.is_set() else ("busy" if active else "idle"),
                "current_jobs": ",".join(job.id for job in active),
                "processed": self.processed,
                "failures": self.failures,
            },
        )
        for job in active:
            if not self.q.renew(job):
                log.warning(
                    "lease lost; the result of this run will be refused",
                    extra={"event": "lease_lost", "job_id": job.id, "worker_id": self.id},
                )

    def _housekeeping(self) -> None:
        last_beat = time.monotonic()
        while not self.stopping.wait(self.s.maintenance_seconds):
            try:
                if time.monotonic() - last_beat >= self.s.heartbeat_seconds:
                    self._heartbeat()
                    self.q.trim_indexes()
                    last_beat = time.monotonic()
                self.q.promote_due()
                recovered = self.q.recover_expired()
                if recovered:
                    log.warning(
                        "recovered jobs with expired leases",
                        extra={"event": "recovered", "count": recovered, "worker_id": self.id},
                    )
                self.q.prune_workers()
            except redis.RedisError as exc:
                log.error("housekeeping failed: %s", exc, extra={"worker_id": self.id})

    # Running jobs

    def _slot_loop(self) -> None:
        pause = 0.5
        while not self.stopping.is_set():
            try:
                job = self.q.claim(self.id)
                if job is None:
                    self.q.wait_for_work(self.s.idle_wait_seconds)
                    continue
                pause = 0.5
            except redis.RedisError as exc:
                log.error("cannot reach Redis: %s", exc, extra={"worker_id": self.id})
                self.stopping.wait(pause)
                pause = min(pause * 2, 10.0)
                continue
            with self._lock:
                self._active[job.id] = job
            try:
                self.execute(job)
            finally:
                with self._lock:
                    self._active.pop(job.id, None)

    def _count(self, failed: bool) -> None:
        with self._lock:
            self.processed += 1
            self.failures += int(failed)

    def execute(self, job: Job) -> str | None:
        """Runs one claimed job and records the outcome. Returns the new state, or None."""
        extra: dict[str, Any] = {
            "job_id": job.id,
            "job_type": job.type,
            "worker_id": self.id,
            "attempt": job.attempts,
        }
        log.info("job started", extra={**extra, "event": "started"})
        start = time.perf_counter()
        retryable = True
        try:
            spec = get_spec(job.type)
            payload = spec.payload_model.model_validate(job.payload)
            result = spec.run(payload, JobContext(job.id, job.attempts, job.max_attempts))
            duration = int((time.perf_counter() - start) * 1000)
            try:
                done = self.q.complete(job, result, duration)
            except ResultTooLargeError as exc:
                raise PermanentJobError(f"result is too large ({exc})") from None
            except redis.RedisError:
                log.exception("could not save result", extra={**extra, "event": "save_failed"})
                return None
            self._count(failed=False)
            if not done:
                log.warning("result refused: lease was lost", extra={**extra, "event": "refused"})
                return None
            log.info(
                "job succeeded", extra={**extra, "event": "succeeded", "duration_ms": duration}
            )
            return "succeeded"
        except (UnknownJobTypeError, ValidationError, PermanentJobError) as exc:
            retryable = False
            error = describe_error(exc)
        except Exception as exc:
            error = describe_error(exc)

        duration = int((time.perf_counter() - start) * 1000)
        self._count(failed=True)
        delay = retry_delay(job.attempts, self.s.retry_base_seconds, self.s.retry_max_seconds)
        try:
            outcome = self.q.fail(job, error, retryable, delay, duration)
        except redis.RedisError:
            log.exception("could not save failure", extra={**extra, "event": "save_failed"})
            return None
        log.warning(
            "job failed: %s",
            error,
            extra={**extra, "event": outcome or "refused", "duration_ms": duration},
        )
        return {"retry": "retry_scheduled", "dead": "dead_letter"}.get(outcome or "", outcome)
