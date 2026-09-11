"""Measures throughput and latency with 1, 3, and 5 workers.

Run from the project root:
    python -m scripts.benchmark --workers 1 3 5 --trials 3

Each trial starts fresh worker processes against its own key prefix, runs a short
warm-up, submits all jobs at once, and waits until every job has finished.
Times come from the Redis server clock, so all processes share one clock.
"""

import argparse
import csv
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis

from app.core.config import Settings
from app.observability.metrics import percentile
from app.queue.models import TERMINAL_STATES, JobState
from app.queue.redis_queue import RedisQueue

ROOT = Path(__file__).resolve().parent.parent

WORKLOADS: dict[str, tuple[str, dict[str, Any]]] = {
    "sleep": ("sleep", {"seconds": 0.1}),
    "cpu": ("cpu", {"n": 300_000}),
}
FIELDS = [
    "run_id",
    "workload",
    "model",
    "workers",
    "trial",
    "job_id",
    "state",
    "attempts",
    "created_at",
    "started_at",
    "finished_at",
    "queue_wait_ms",
    "run_ms",
    "end_to_end_ms",
]


@dataclass
class TrialSummary:
    workload: str
    model: str
    workers: int
    trial: int
    submitted: int
    succeeded: int
    failed: int
    retried: int
    dead_letter: int
    elapsed_s: float
    throughput_per_s: float
    queue_wait_p50_ms: float | None
    queue_wait_p95_ms: float | None
    end_to_end_p50_ms: float | None
    end_to_end_p95_ms: float | None
    run_p50_ms: float | None


def job_rows(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds latency columns. Jobs that never started or finished get None."""
    rows = []
    for job in jobs:
        created, started, finished = job["created_at"], job["started_at"], job["finished_at"]
        rows.append(
            {
                **job,
                "queue_wait_ms": started - created if started else None,
                "run_ms": finished - started if started and finished else None,
                "end_to_end_ms": finished - created if finished else None,
            }
        )
    return rows


def summarize(
    rows: list[dict[str, Any]], stats: dict[str, int], meta: dict[str, Any]
) -> TrialSummary:
    done = [r for r in rows if r["state"] == JobState.SUCCEEDED]
    waits = [r["queue_wait_ms"] for r in done]
    totals = [r["end_to_end_ms"] for r in done]
    runs = [r["run_ms"] for r in done]
    if done:
        start = min(r["created_at"] for r in rows)
        end = max(r["finished_at"] for r in done)
        elapsed = max((end - start) / 1000, 0.001)
    else:
        elapsed = 0.0
    return TrialSummary(
        workload=meta["workload"],
        model=meta["model"],
        workers=meta["workers"],
        trial=meta["trial"],
        submitted=len(rows),
        succeeded=len(done),
        failed=sum(1 for r in rows if r["state"] == JobState.FAILED),
        retried=stats.get("total_retried", 0),
        dead_letter=sum(1 for r in rows if r["state"] == JobState.DEAD_LETTER),
        elapsed_s=round(elapsed, 3),
        throughput_per_s=round(len(done) / elapsed, 2) if elapsed else 0.0,
        queue_wait_p50_ms=percentile(waits, 50),
        queue_wait_p95_ms=percentile(waits, 95),
        end_to_end_p50_ms=percentile(totals, 50),
        end_to_end_p95_ms=percentile(totals, 95),
        run_p50_ms=percentile(runs, 50),
    )


def median_by_config(trials: list[TrialSummary]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, int], list[TrialSummary]] = {}
    for t in trials:
        groups.setdefault((t.workload, t.model, t.workers), []).append(t)
    out = []
    for (workload, model, workers), items in groups.items():

        def med(name: str, items: list[TrialSummary] = items) -> float | None:
            values = [getattr(i, name) for i in items if getattr(i, name) is not None]
            return round(statistics.median(values), 2) if values else None

        out.append(
            {
                "workload": workload,
                "model": model,
                "workers": workers,
                "trials": len(items),
                "succeeded": sum(i.succeeded for i in items),
                "submitted": sum(i.submitted for i in items),
                "throughput_per_s": med("throughput_per_s"),
                "end_to_end_p50_ms": med("end_to_end_p50_ms"),
                "end_to_end_p95_ms": med("end_to_end_p95_ms"),
                "queue_wait_p95_ms": med("queue_wait_p95_ms"),
                "run_p50_ms": med("run_p50_ms"),
            }
        )
    return out


def markdown_table(rows: list[dict[str, Any]]) -> str:
    head = (
        "| Workload | Model | Workers | Jobs done | Throughput (jobs/s) "
        "| End-to-end p50 (ms) | End-to-end p95 (ms) | Run time p50 (ms) |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = [
        f"| {r['workload']} | {r['model']} | {r['workers']} | {r['succeeded']}/{r['submitted']} "
        f"| {r['throughput_per_s']} | {r['end_to_end_p50_ms']} | {r['end_to_end_p95_ms']} "
        f"| {r['run_p50_ms']} |"
        for r in rows
    ]
    return head + "\n".join(lines) + "\n"


# Running trials


def environment(client: redis.Redis) -> dict[str, Any]:
    info = client.info("server")
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "redis": info.get("redis_version"),
        "project_version": "1.0.0",
    }


def start_workers(model: str, count: int, env: dict[str, str]) -> list[subprocess.Popen[bytes]]:
    # mypy checks one platform at a time and understands sys.platform, not os.name.
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags = 0
    if model == "process":
        commands = [["--id", f"bench-{i}", "--concurrency", "1"] for i in range(count)]
    else:
        commands = [["--id", "bench-0", "--concurrency", str(count)]]
    return [
        subprocess.Popen(
            [sys.executable, "-m", "app.workers", *args],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        for args in commands
    ]


def stop_workers(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        if p.poll() is None:
            if sys.platform == "win32":
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def wait_all(queue: RedisQueue, ids: list[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    pending = list(ids)
    while pending:
        if time.monotonic() > deadline:
            raise TimeoutError(f"{len(pending)} jobs did not finish in {timeout} s")
        pipe = queue.r.pipeline(transaction=False)
        for job_id in pending:
            pipe.hget(queue.key(f"job:{job_id}"), "state")
        states = pipe.execute()
        pending = [i for i, s in zip(pending, states, strict=True) if s not in TERMINAL_STATES]
        if pending:
            time.sleep(0.05)


def run_trial(
    client: redis.Redis, args: argparse.Namespace, workload: str, model: str, workers: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prefix = f"bench:{uuid.uuid4().hex[:10]}:"
    settings = Settings(_env_file=None, redis_url=args.redis_url, key_prefix=prefix)  # type: ignore[call-arg]
    queue = RedisQueue(client, settings)
    env = {**os.environ, "REDIS_URL": args.redis_url, "KEY_PREFIX": prefix, "LOG_LEVEL": "WARNING"}
    procs = start_workers(model, workers, env)
    try:
        deadline = time.monotonic() + 30
        while len([w for w in queue.workers() if w["alive"]]) < len(procs):
            if time.monotonic() > deadline:
                raise TimeoutError("workers did not start")
            time.sleep(0.1)

        warmup = [queue.enqueue("add", {"a": 1, "b": 1}).id for _ in range(workers * 2)]
        wait_all(queue, warmup, timeout=60)
        before = queue.stats()

        job_type, payload = WORKLOADS[workload]
        ids = [queue.enqueue(job_type, payload).id for _ in range(args.jobs)]
        wait_all(queue, ids, timeout=args.timeout)

        after = queue.stats()
        stats = {k: after[k] - before[k] for k in after if k.startswith("total_")}
        jobs = []
        for job_id in ids:
            job = queue.get(job_id, with_history=False)
            assert job is not None
            jobs.append(
                {
                    "job_id": job.id,
                    "state": job.state.value,
                    "attempts": job.attempts,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                }
            )
        return job_rows(jobs), stats
    finally:
        stop_workers(procs)
        keys = list(client.scan_iter(match=f"{prefix}*", count=1000))
        if keys:
            client.delete(*keys)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument(
        "--workloads", nargs="+", choices=sorted(WORKLOADS), default=["sleep", "cpu"]
    )
    parser.add_argument("--models", nargs="+", choices=["process", "thread"], default=["process"])
    parser.add_argument("--jobs", type=int, default=200, help="jobs per trial")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600, help="seconds per trial")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("BENCH_REDIS_URL", "redis://127.0.0.1:6379/13"),
        help="use a database that holds no real data",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "results")
    parser.add_argument("--note", default="", help="test conditions to save with the results")
    args = parser.parse_args()

    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    client.ping()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / run_id
    out.mkdir(parents=True, exist_ok=True)

    trials: list[TrialSummary] = []
    with (out / "raw.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for workload in args.workloads:
            for model in args.models:
                for workers in args.workers:
                    for trial in range(1, args.trials + 1):
                        meta = {"workload": workload, "model": model, "workers": workers}
                        rows, stats = run_trial(client, args, workload, model, workers)
                        for row in rows:
                            writer.writerow({"run_id": run_id, **meta, "trial": trial, **row})
                        summary = summarize(rows, stats, {**meta, "trial": trial})
                        trials.append(summary)
                        print(
                            f"{workload:5} {model:7} workers={workers} trial={trial} "
                            f"done={summary.succeeded}/{summary.submitted} "
                            f"{summary.throughput_per_s} jobs/s "
                            f"p95={summary.end_to_end_p95_ms} ms",
                            flush=True,
                        )

    medians = median_by_config(trials)
    report = {
        "run_id": run_id,
        "environment": environment(client),
        "note": args.note,
        "settings": {
            "jobs_per_trial": args.jobs,
            "trials": args.trials,
            "warmup": "2 add jobs per worker slot, not measured",
            "workloads": {k: {"type": v[0], "payload": v[1]} for k, v in WORKLOADS.items()},
            "models": {
                "process": "N worker processes, one job at a time each",
                "thread": "1 worker process with N threads",
            },
        },
        "medians": medians,
        "trials": [asdict(t) for t in trials],
    }
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown_table(medians), encoding="utf-8")
    client.close()
    print(f"\nMedian of {args.trials} trials:\n")
    print(markdown_table(medians))
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
