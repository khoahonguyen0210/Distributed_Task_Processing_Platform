import argparse
import csv
import json
import statistics
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.observability.metrics import percentile
from scripts.cluster import (
    Api,
    Sample,
    Sampler,
    deployment_replicas,
    machine,
    set_replicas,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent
WORKERS = "worker"
JOB_FIELDS = [
    "mode",
    "trial",
    "job_id",
    "state",
    "attempts",
    "created_at",
    "started_at",
    "finished_at",
    "queue_wait_ms",
    "end_to_end_ms",
]
SAMPLE_FIELDS = ["mode", "trial", "t", "replicas", "ready", "alive", "backlog"]


def replica_seconds(samples: list[Sample], start: float, end: float) -> float:
    points = [s for s in samples if start <= s.t <= end]
    total = 0.0
    for a, b in zip(points, points[1:], strict=False):
        total += a.replicas * (b.t - a.t)
    if points:
        total += points[-1].replicas * (end - points[-1].t)
    return round(total, 1)


def prepare(mode: str, api: Api) -> None:
    if mode == "keda":
        set_replicas(WORKERS, None)
        ok = wait_until(
            lambda: deployment_replicas(WORKERS)[0] == 0 and api.alive_workers() == 0, 400, 2
        )
    else:
        count = int(mode.removeprefix("fixed-"))
        set_replicas(WORKERS, count)
        ok = wait_until(
            lambda: deployment_replicas(WORKERS) == (count, count) and api.alive_workers() == count,
            400,
            2,
        )
    if not ok:
        raise TimeoutError(f"workers did not settle for mode {mode}")


def job_rows(jobs: list[dict[str, Any] | None], ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job_id, job in zip(ids, jobs, strict=True):
        if job is None:
            rows.append({"job_id": job_id, "state": "missing"})
            continue
        created, started, finished = job["created_at"], job["started_at"], job["finished_at"]
        rows.append(
            {
                "job_id": job_id,
                "state": job["state"],
                "attempts": job["attempts"],
                "created_at": created,
                "started_at": started,
                "finished_at": finished,
                "queue_wait_ms": started - created if started is not None else None,
                "end_to_end_ms": finished - created if finished is not None else None,
            }
        )
    return rows


def summarize(
    mode: str, trial: int, rows: list[dict[str, Any]], samples: list[Sample], window: float
) -> dict[str, Any]:
    done = [r for r in rows if r["state"] == "succeeded"]
    ends = [r["end_to_end_ms"] for r in done]
    waits = [r["queue_wait_ms"] for r in done]
    created = [r["created_at"] for r in rows if r.get("created_at") is not None]
    started = [r["started_at"] for r in rows if r.get("started_at") is not None]
    finished = [r["finished_at"] for r in done]
    first_ready = next((s.t for s in samples if s.t >= 0 and s.ready > 0), None)
    finish_s = (max(finished) - min(created)) / 1000 if finished and created else None
    zero_at = None
    if finish_s is not None:
        zero_at = next((s.t for s in samples if s.t > finish_s and s.replicas == 0), None)
    return {
        "mode": mode,
        "trial": trial,
        "submitted": len(rows),
        "succeeded": len(done),
        "failed": sum(1 for r in rows if r["state"] == "failed"),
        "dead_letter": sum(1 for r in rows if r["state"] == "dead_letter"),
        "lost": sum(1 for r in rows if r["state"] not in {"succeeded", "failed", "dead_letter"}),
        "retried_runs": sum(max(0, int(r.get("attempts") or 0) - 1) for r in rows),
        "completion_s": round(finish_s, 2) if finish_s is not None else None,
        "end_to_end_p50_s": _seconds(percentile(ends, 50)),
        "end_to_end_p95_s": _seconds(percentile(ends, 95)),
        "queue_wait_p95_s": _seconds(percentile(waits, 95)),
        "first_job_start_s": round((min(started) - min(created)) / 1000, 2) if started else None,
        "first_ready_worker_s": first_ready,
        "peak_replicas": max((s.replicas for s in samples), default=0),
        "replica_seconds": replica_seconds(samples, 0, window),
        "back_to_zero_s": zero_at,
    }


def _seconds(ms: float | None) -> float | None:
    return round(ms / 1000, 2) if ms is not None else None


def run_trial(
    api: Api, args: argparse.Namespace, mode: str, trial: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Sample], float]:
    prepare(mode, api)
    start = time.time() + args.idle_before
    sampler = Sampler(api, start, WORKERS)
    sampler.start()
    time.sleep(args.idle_before)
    base = api.finished_total()
    responses = api.submit_many(args.jobs, "sleep", {"seconds": args.seconds})
    ids = [r.json()["id"] for r in responses if r.status_code == 202]
    if len(ids) != args.jobs:
        raise RuntimeError(f"only {len(ids)} of {args.jobs} jobs were accepted")
    deadline = start + args.timeout
    while time.time() < deadline and (
        api.finished_total() - base < len(ids) or time.time() < start + args.window
    ):
        time.sleep(2)
    samples = sampler.stop()
    rows = job_rows(api.jobs(ids), ids)
    summary = summarize(mode, trial, rows, samples, args.window)
    return summary, rows, samples, sampler.max_gap


def medians(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for mode in dict.fromkeys(t["mode"] for t in trials):
        items = [t for t in trials if t["mode"] == mode]
        row: dict[str, Any] = {"mode": mode, "trials": len(items)}
        for key in (
            "completion_s",
            "end_to_end_p50_s",
            "end_to_end_p95_s",
            "first_job_start_s",
            "first_ready_worker_s",
            "replica_seconds",
            "back_to_zero_s",
        ):
            values = [t[key] for t in items if t[key] is not None]
            row[key] = round(statistics.median(values), 2) if values else None
        row["succeeded"] = sum(t["succeeded"] for t in items)
        row["submitted"] = sum(t["submitted"] for t in items)
        row["lost"] = sum(t["lost"] for t in items)
        row["dead_letter"] = sum(t["dead_letter"] for t in items)
        out.append(row)
    return out


def markdown_table(rows: list[dict[str, Any]]) -> str:
    head = (
        "| Workers | Jobs done | Lost | Finish time (s) | End-to-end p50 (s) "
        "| End-to-end p95 (s) | First job start (s) | Worker-seconds | Back to zero (s) |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = [
        f"| {r['mode']} | {r['succeeded']}/{r['submitted']} | {r['lost']} | {r['completion_s']} "
        f"| {r['end_to_end_p50_s']} | {r['end_to_end_p95_s']} | {r['first_job_start_s']} "
        f"| {r['replica_seconds']} | {r['back_to_zero_s'] if r['back_to_zero_s'] else '-'} |"
        for r in rows
    ]
    return head + "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare KEDA autoscaling with fixed worker counts on one bursty workload."
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--modes", nargs="+", default=["keda", "fixed-1", "fixed-3", "fixed-5"])
    parser.add_argument("--jobs", type=int, default=120, help="jobs in the burst")
    parser.add_argument("--seconds", type=float, default=1.0, help="length of each sleep job")
    parser.add_argument("--idle-before", type=float, default=10)
    parser.add_argument("--window", type=float, default=200, help="seconds measured per trial")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "autoscaling")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    api = Api(args.api)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / run_id
    out.mkdir(parents=True, exist_ok=True)
    trials: list[dict[str, Any]] = []
    with (
        (out / "jobs.csv").open("w", newline="", encoding="utf-8") as jobs_file,
        (out / "samples.csv").open("w", newline="", encoding="utf-8") as samples_file,
    ):
        jobs_csv = csv.DictWriter(jobs_file, fieldnames=JOB_FIELDS)
        samples_csv = csv.DictWriter(samples_file, fieldnames=SAMPLE_FIELDS)
        jobs_csv.writeheader()
        samples_csv.writeheader()
        for trial in range(1, args.trials + 1):
            for mode in args.modes:
                for _ in range(3):
                    summary, rows, samples, gap = run_trial(api, args, mode, trial)
                    if gap < 5:
                        break
                    print(f"  {mode} trial {trial}: clock gap of {gap:.0f} s, running it again")
                else:
                    raise RuntimeError("the machine kept pausing; no valid trial")
                trials.append(summary)
                for row in rows:
                    jobs_csv.writerow({"mode": mode, "trial": trial, **row})
                for s in samples:
                    samples_csv.writerow({"mode": mode, "trial": trial, **asdict(s)})
                jobs_file.flush()
                samples_file.flush()
                print(
                    f"{mode:8} trial={trial} done={summary['succeeded']}/{summary['submitted']} "
                    f"lost={summary['lost']} finish={summary['completion_s']} s "
                    f"p95={summary['end_to_end_p95_s']} s "
                    f"worker-s={summary['replica_seconds']}",
                    flush=True,
                )
    set_replicas(WORKERS, None)

    table = medians(trials)
    report = {
        "run_id": run_id,
        "machine": machine(),
        "note": args.note,
        "workload": {
            "idle_before_s": args.idle_before,
            "burst": f"{args.jobs} sleep jobs of {args.seconds} s, sent at once",
            "window_s": args.window,
            "worker": "1 process per pod, 1 job at a time",
            "modes": "keda = KEDA 0 to 5 replicas; fixed-N = autoscaling paused at N replicas",
        },
        "medians": table,
        "trials": trials,
    }
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown_table(table), encoding="utf-8")
    api.close()
    print("\n" + markdown_table(table))
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
