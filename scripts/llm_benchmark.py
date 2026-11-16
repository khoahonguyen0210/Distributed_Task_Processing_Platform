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
from scripts.burst_benchmark import replica_seconds
from scripts.cluster import (
    Api,
    PortForward,
    Sample,
    Sampler,
    deployment_replicas,
    kubectl,
    machine,
    set_api_env,
    set_replicas,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent
WORKERS = "worker"
SERVERS = "model-server"
PROMPTS = [
    "Explain what a job queue is in two sentences.",
    "Give three tips for writing a clear email.",
    "Describe a sunny morning in a small town.",
    "List four ways to save energy at home.",
    "Explain why the sky looks blue.",
    "Write a short note to thank a friend for a gift.",
    "Give two reasons to back up computer files.",
    "Describe how to make a cup of tea.",
    "Explain what a computer network is.",
    "Give three ideas for a weekend walk.",
    "Explain what a password manager does.",
    "Describe the sound of rain on a roof.",
]
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
    "completion_tokens",
]
SAMPLE_FIELDS = ["mode", "trial", "t", "replicas", "ready", "alive", "backlog"]


def parse_mode(mode: str) -> tuple[int | None, bool]:
    name = mode.removesuffix("-open")
    servers = None if name == "auto" else int(name.removeprefix("fixed-"))
    return servers, not mode.endswith("-open")


def prepare(api: Api, args: argparse.Namespace, servers: int | None) -> None:
    set_replicas(WORKERS, args.workers)
    set_replicas(SERVERS, servers)
    want = servers or 1

    def settled() -> bool:
        workers_ok = deployment_replicas(WORKERS) == (args.workers, args.workers)
        servers_ok = deployment_replicas(SERVERS) == (want, want)
        return workers_ok and servers_ok and api.alive_workers() == args.workers

    if not wait_until(settled, 600, 2):
        raise TimeoutError("the worker or model server count did not become steady")


def job_rows(jobs: list[dict[str, Any] | None], ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job_id, job in zip(ids, jobs, strict=True):
        if job is None:
            rows.append({"job_id": job_id, "state": "missing"})
            continue
        created, started, finished = job["created_at"], job["started_at"], job["finished_at"]
        result = job.get("result") or {}
        rows.append(
            {
                "job_id": job_id,
                "state": job["state"],
                "attempts": job["attempts"],
                "created_at": created,
                "started_at": started,
                "finished_at": finished,
                "queue_wait_ms": started - created if started is not None else None,
                "end_to_end_ms": (
                    finished - created
                    if finished is not None and job["state"] == "succeeded"
                    else None
                ),
                "completion_tokens": result.get("completion_tokens"),
            }
        )
    return rows


def summarize(
    mode: str,
    trial: int,
    offered: int,
    rows: list[dict[str, Any]],
    samples: list[Sample],
    window: float,
) -> dict[str, Any]:
    done = [r for r in rows if r["state"] == "succeeded"]
    ends = [r["end_to_end_ms"] for r in done]
    waits = [r["queue_wait_ms"] for r in done]
    tokens = sum(int(r["completion_tokens"] or 0) for r in done)
    span = 0.0
    if done:
        span = (max(r["finished_at"] for r in done) - min(r["started_at"] for r in done)) / 1000
    first_ready = samples[0].ready if samples else 0
    first_replicas = samples[0].replicas if samples else 0
    scale_up = next((s.t for s in samples if s.t >= 0 and s.ready > first_ready), None)
    scale_down = None
    if scale_up is not None:
        scale_down = next(
            (s.t for s in samples if s.t > scale_up and s.replicas <= first_replicas), None
        )

    def count(state: str) -> int:
        return sum(1 for r in rows if r["state"] == state)

    final = {"succeeded", "failed", "dead_letter", "expired", "cancelled"}
    return {
        "mode": mode,
        "trial": trial,
        "offered": offered,
        "accepted": len(rows),
        "rejected": offered - len(rows),
        "succeeded": len(done),
        "expired": count("expired"),
        "failed": count("failed"),
        "dead_letter": count("dead_letter"),
        "lost": sum(1 for r in rows if r["state"] not in final),
        "retried_runs": sum(max(0, int(r.get("attempts") or 0) - 1) for r in rows),
        "end_to_end_p50_s": seconds(percentile(ends, 50)),
        "end_to_end_p95_s": seconds(percentile(ends, 95)),
        "end_to_end_p99_s": seconds(percentile(ends, 99)),
        "queue_wait_p95_s": seconds(percentile(waits, 95)),
        "output_tokens_per_s": round(tokens / span, 1) if done and span > 0 else None,
        "peak_servers": max((s.replicas for s in samples), default=0),
        "server_seconds": replica_seconds(samples, 0, window),
        "first_scale_up_s": scale_up,
        "scale_down_s": scale_down,
    }


def seconds(ms: float | None) -> float | None:
    return round(ms / 1000, 2) if ms is not None else None


def run_trial(
    api: Api, args: argparse.Namespace, mode: str, trial: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Sample], float]:
    servers, _ = parse_mode(mode)
    prepare(api, args, servers)
    start = time.time() + args.idle_before
    sampler = Sampler(api, start, SERVERS)
    sampler.start()
    time.sleep(max(0.0, start - time.time()))
    offered = round(args.rate * args.duration)
    base = api.finished_total()
    ids: list[str] = []
    for i in range(offered):
        time.sleep(max(0.0, start + i / args.rate - time.time()))
        payload = {"prompt": PROMPTS[i % len(PROMPTS)], "max_tokens": args.max_tokens}
        response = api.submit("llm_generate", payload, deadline_seconds=args.deadline)
        if response.status_code == 202:
            ids.append(response.json()["id"])
        elif response.status_code != 429:
            raise RuntimeError(f"unexpected answer {response.status_code}: {response.text}")
    base_deadline = start + args.timeout
    while time.time() < base_deadline and (
        api.finished_total() - base < len(ids) or time.time() < start + args.window
    ):
        time.sleep(3)
    samples = sampler.stop()
    rows = job_rows(api.jobs(ids), ids)
    return (
        summarize(mode, trial, offered, rows, samples, args.window),
        rows,
        samples,
        sampler.max_gap,
    )


def medians(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for mode in dict.fromkeys(t["mode"] for t in trials):
        items = [t for t in trials if t["mode"] == mode]
        row: dict[str, Any] = {"mode": mode, "trials": len(items)}
        for key in (
            "end_to_end_p50_s",
            "end_to_end_p95_s",
            "end_to_end_p99_s",
            "queue_wait_p95_s",
            "output_tokens_per_s",
            "server_seconds",
            "first_scale_up_s",
            "scale_down_s",
        ):
            values = [t[key] for t in items if t[key] is not None]
            row[key] = round(statistics.median(values), 2) if values else None
        for key in ("offered", "accepted", "rejected", "succeeded", "expired", "lost"):
            row[key] = sum(t[key] for t in items)
        row["dead_letter"] = sum(t["dead_letter"] + t["failed"] for t in items)
        out.append(row)
    return out


def markdown_table(rows: list[dict[str, Any]]) -> str:
    head = (
        "| Model servers | Sent | Rejected (429) | Expired | Succeeded | Lost "
        "| p50 (s) | p95 (s) | p99 (s) | Tokens/s | Server-seconds | First scale-up (s) |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = [
        f"| {r['mode']} | {r['offered']} | {r['rejected']} | {r['expired']} | {r['succeeded']} "
        f"| {r['lost']} | {r['end_to_end_p50_s']} | {r['end_to_end_p95_s']} "
        f"| {r['end_to_end_p99_s']} | {r['output_tokens_per_s']} | {r['server_seconds']} "
        f"| {r['first_scale_up_s'] if r['first_scale_up_s'] is not None else '-'} |"
        for r in rows
    ]
    return head + "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send bursts of LLM jobs with fixed and autoscaled model servers."
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["fixed-1", "fixed-2", "auto", "fixed-2-open", "auto-open"],
        help="fixed-N or auto; add -open to turn the queue limit off",
    )
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--rate", type=float, default=0.4, help="jobs sent per second")
    parser.add_argument("--duration", type=float, default=60, help="seconds of sending")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--deadline", type=float, default=60)
    parser.add_argument("--queue-limit", type=int, default=3, help="MAX_QUEUE_DEPTH when on")
    parser.add_argument("--idle-before", type=float, default=15)
    parser.add_argument("--window", type=float, default=240)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "inference")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    forward = PortForward("api", args.port, 8000)
    forward.start()
    api = Api(forward.url)
    current_limit: int | None = None
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / run_id
    out.mkdir(parents=True, exist_ok=True)
    trials: list[dict[str, Any]] = []
    try:
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
                    limit = args.queue_limit if parse_mode(mode)[1] else 0
                    if limit != current_limit:
                        set_api_env("MAX_QUEUE_DEPTH", str(limit))
                        forward.start()
                        current_limit = limit
                    for _ in range(3):
                        summary, rows, samples, gap = run_trial(api, args, mode, trial)
                        if gap < 5:
                            break
                        print(f"  {mode} trial {trial}: clock gap of {gap:.0f} s, running again")
                    else:
                        raise RuntimeError("the machine kept pausing; no valid trial")
                    trials.append(summary)
                    for row in rows:
                        jobs_csv.writerow({"mode": mode, "trial": trial, **row})
                    for s in samples:
                        samples_csv.writerow({"mode": mode, "trial": trial, **asdict(s)})
                    jobs_file.flush()
                    samples_file.flush()
                    print(json.dumps(summary), flush=True)
    finally:
        set_replicas(SERVERS, None)
        set_replicas(WORKERS, None)
        if current_limit is not None:
            set_api_env("MAX_QUEUE_DEPTH", None)

    table = medians(trials)
    server_args = kubectl(
        "get", "deploy", SERVERS, "-o", "jsonpath={.spec.template.spec.containers[0].args}"
    )
    report = {
        "run_id": run_id,
        "machine": machine(),
        "note": args.note,
        "model_server": json.loads(server_args) if server_args else None,
        "workload": {
            "idle_before_s": args.idle_before,
            "burst": f"{args.rate} llm_generate jobs per second for {args.duration} s",
            "max_tokens": args.max_tokens,
            "prompts": PROMPTS,
            "deadline_s": args.deadline,
            "queue_limit_when_on": args.queue_limit,
            "workers": f"{args.workers} pods, 2 jobs at a time each",
            "window_s": args.window,
        },
        "medians": table,
        "trials": trials,
    }
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown_table(table), encoding="utf-8")
    api.close()
    forward.stop()
    print("\n" + markdown_table(table))
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
