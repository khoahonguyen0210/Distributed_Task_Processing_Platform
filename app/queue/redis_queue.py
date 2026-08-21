import json
import secrets
import time
import uuid
from functools import cache
from importlib import resources
from typing import Any

import redis

from app.core.config import Settings
from app.queue.models import Job, JobState

JSON_SEPARATORS = (",", ":")


class PayloadTooLargeError(ValueError):
    pass


class ResultTooLargeError(ValueError):
    pass


@cache
def _lua_source(name: str) -> str:
    folder = resources.files("app.queue") / "lua"
    common = (folder / "_common.lua").read_text(encoding="utf-8")
    return common + "\n" + (folder / f"{name}.lua").read_text(encoding="utf-8")


def encode_json(value: Any, limit: int, error: type[ValueError]) -> str:
    text = json.dumps(value, separators=JSON_SEPARATORS, ensure_ascii=False)
    if len(text.encode("utf-8")) > limit:
        raise error(f"encoded size is over the {limit}-byte limit")
    return text


def new_job_id() -> str:
    return uuid.uuid4().hex


class RedisQueue:
    """All job state lives in Redis. Every change of state is one Lua script, so it is atomic."""

    SCRIPTS = (
        "enqueue",
        "claim",
        "complete",
        "fail",
        "renew",
        "cancel",
        "replay",
        "promote",
        "recover",
        "release",
    )

    def __init__(self, client: "redis.Redis", settings: Settings) -> None:
        self.r = client
        self.s = settings
        self.p = settings.key_prefix
        self._scripts = {name: client.register_script(_lua_source(name)) for name in self.SCRIPTS}

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisQueue":
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=max(5.0, settings.idle_wait_seconds + 5),
            health_check_interval=30,
        )
        return cls(client, settings)

    def close(self) -> None:
        self.r.close()

    def _run(self, name: str, *args: Any) -> Any:
        return self._scripts[name](keys=[], args=[self.p, *args])

    def key(self, name: str) -> str:
        return self.p + name

    def ping(self) -> bool:
        return bool(self.r.ping())

    # Submitting and reading jobs

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        priority: int = 5,
        delay_seconds: float = 0,
        max_attempts: int | None = None,
    ) -> Job:
        if not 0 <= priority <= 9:
            raise ValueError("priority must be between 0 and 9")
        body = encode_json(payload, self.s.max_payload_bytes, PayloadTooLargeError)
        attempts = max_attempts or self.s.default_max_attempts
        job_id = new_job_id()
        self._run(
            "enqueue", job_id, job_type, body, priority, attempts, int(max(delay_seconds, 0) * 1000)
        )
        job = self.get(job_id, with_history=False)
        assert job is not None
        return job

    def get(self, job_id: str, with_history: bool = True) -> Job | None:
        data = self.r.hgetall(self.key(f"job:{job_id}"))
        if not data:
            return None
        job = Job.from_hash(data)
        if with_history:
            job.history = self.history(job_id)
        return job

    def history(self, job_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(item) for item in self.r.lrange(self.key(f"job:{job_id}:history"), 0, -1)
        ]

    def list_jobs(
        self,
        state: JobState | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """Newest first. Filters scan at most `list_scan_limit` recent jobs."""
        index = self.key("jobs")
        found: list[Job] = []
        skipped = 0
        start, chunk = 0, 200
        while start < self.s.list_scan_limit and len(found) < limit:
            ids = self.r.zrevrange(index, start, start + chunk - 1)
            if not ids:
                break
            start += chunk
            pipe = self.r.pipeline(transaction=False)
            for job_id in ids:
                pipe.hgetall(self.key(f"job:{job_id}"))
            gone = []
            for job_id, data in zip(ids, pipe.execute(), strict=True):
                if not data:
                    gone.append(job_id)
                    continue
                job = Job.from_hash(data)
                if (state and job.state != state) or (job_type and job.type != job_type):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                found.append(job)
                if len(found) >= limit:
                    break
            if gone:
                self.r.zrem(index, *gone)
        return found

    def dead_letters(self, limit: int = 50, offset: int = 0) -> list[Job]:
        ids = self.r.zrevrange(self.key("dead"), offset, offset + limit - 1)
        jobs = [self.get(job_id, with_history=False) for job_id in ids]
        return [job for job in jobs if job is not None]

    # Worker side

    def claim(self, worker_id: str) -> Job | None:
        token = secrets.token_hex(16)
        lease_ms = int(self.s.lease_seconds * 1000)
        flat = self._run("claim", worker_id, lease_ms, token)
        if not flat:
            return None
        return Job.from_hash(dict(zip(flat[::2], flat[1::2], strict=True)))

    def complete(self, job: Job, result: Any, duration_ms: int) -> bool:
        body = encode_json(result, self.s.max_result_bytes, ResultTooLargeError)
        return bool(
            self._run(
                "complete", job.id, job.lease_token, body, self.s.job_ttl_seconds, duration_ms
            )
        )

    def fail(
        self, job: Job, error: str, retryable: bool, retry_delay: float, duration_ms: int
    ) -> str | None:
        outcome = self._run(
            "fail",
            job.id,
            job.lease_token,
            error[: self.s.max_error_chars],
            1 if retryable else 0,
            int(retry_delay * 1000),
            self.s.job_ttl_seconds,
            self.s.dead_letter_ttl_seconds,
            duration_ms,
        )
        return str(outcome) if outcome else None

    def renew(self, job: Job) -> bool:
        lease_ms = int(self.s.lease_seconds * 1000)
        return bool(self._run("renew", job.id, job.lease_token, lease_ms))

    def release(self, job: Job) -> bool:
        return bool(self._run("release", job.id, job.lease_token))

    def wait_for_work(self, timeout: float) -> None:
        self.r.blpop([self.key("wake")], timeout=timeout)

    # Maintenance and admin

    def promote_due(self, batch: int = 100) -> int:
        return int(self._run("promote", batch))

    def recover_expired(self, batch: int = 100) -> int:
        return int(self._run("recover", batch, self.s.dead_letter_ttl_seconds))

    def cancel(self, job_id: str) -> tuple[bool, str | None]:
        """Returns (True, "cancelled"), (False, current state), or (False, None) if missing."""
        outcome = self._run("cancel", job_id, self.s.job_ttl_seconds)
        if outcome == 1:
            return True, "cancelled"
        return False, str(outcome) if outcome else None

    def replay(self, job_id: str) -> bool:
        return bool(self._run("replay", job_id))

    def stats(self) -> dict[str, int]:
        pipe = self.r.pipeline(transaction=False)
        pipe.hgetall(self.key("stats"))
        for name in ("ready", "scheduled", "leases", "dead"):
            pipe.zcard(self.key(name))
        counters, ready, scheduled, running, dead = pipe.execute()
        totals = {
            name: int(counters.get(name, 0))
            for name in (
                "submitted",
                "succeeded",
                "failed",
                "retried",
                "dead_letter",
                "cancelled",
                "replayed",
                "recovered",
            )
        }
        return {
            **{f"total_{name}": value for name, value in totals.items()},
            "ready": ready,
            "scheduled": scheduled,
            "running": running,
            "dead_letter": dead,
        }

    # Worker registry

    def worker_heartbeat(self, worker_id: str, info: dict[str, str | int]) -> None:
        now = int(time.time() * 1000)
        pipe = self.r.pipeline(transaction=False)
        pipe.zadd(self.key("workers"), {worker_id: now})
        pipe.hset(self.key(f"worker:{worker_id}"), mapping={**info, "last_seen": now})
        pipe.expire(self.key(f"worker:{worker_id}"), int(self.s.worker_stale_seconds * 4) + 1)
        pipe.execute()

    def remove_worker(self, worker_id: str) -> None:
        self.r.zrem(self.key("workers"), worker_id)
        self.r.delete(self.key(f"worker:{worker_id}"))

    def prune_workers(self) -> int:
        """Forgets long-silent workers. Recently stale ones stay visible."""
        cutoff = int((time.time() - self.s.worker_stale_seconds * 4) * 1000)
        return int(self.r.zremrangebyscore(self.key("workers"), "-inf", cutoff))

    def workers(self) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        stale_ms = self.s.worker_stale_seconds * 1000
        entries = self.r.zrange(self.key("workers"), 0, -1, withscores=True)
        pipe = self.r.pipeline(transaction=False)
        for worker_id, _ in entries:
            pipe.hgetall(self.key(f"worker:{worker_id}"))
        result = []
        for (worker_id, seen), info in zip(entries, pipe.execute(), strict=True):
            result.append(
                {
                    **info,
                    "id": worker_id,
                    "last_seen": int(seen),
                    "alive": now - seen <= stale_ms,
                }
            )
        return result

    def recent_durations(self) -> list[int]:
        return [int(v) for v in self.r.lrange(self.key("durations"), 0, -1)]
