import argparse
import json
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.cluster import (
    Api,
    deployment_replicas,
    kill_pod,
    machine,
    set_replicas,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent
WORKERS = "worker"


def settle_workers(api: Api, count: int) -> None:
    set_replicas(WORKERS, count)
    if not wait_until(
        lambda: deployment_replicas(WORKERS) == (count, count) and api.alive_workers() == count,
        300,
        2,
    ):
        raise TimeoutError(f"{count} workers did not start")


def states(api: Api, ids: list[str]) -> dict[str, int]:
    return dict(Counter(job["state"] if job else "missing" for job in api.jobs(ids)))


def events(api: Api, job_id: str) -> set[str]:
    job = api.job(job_id)
    return {h["event"] for h in job["history"]} if job else set()


def wait_running(
    api: Api, ids: list[str], count: int, timeout: float = 120
) -> list[dict[str, Any]]:
    running: list[dict[str, Any]] = []

    def check() -> bool:
        nonlocal running
        running = [j for j in api.jobs(ids) if j and j["state"] == "running"]
        return len(running) >= count

    if not wait_until(check, timeout, 0.5):
        raise TimeoutError(f"{count} jobs did not start running")
    return running


def finish(api: Api, ids: list[str], timeout: float) -> list[dict[str, Any] | None]:
    wait_until(lambda: not api.unfinished(ids), timeout, 1)
    return api.jobs(ids)


def scale_down(api: Api, args: argparse.Namespace) -> dict[str, Any]:
    settle_workers(api, 5)
    ids = [r.json()["id"] for r in api.submit_many(5, "sleep", {"seconds": args.job_seconds})]
    wait_running(api, ids, 5)
    before = states(api, ids)
    stopped_at = time.time()
    set_replicas(WORKERS, 1)
    jobs = finish(api, ids, args.job_seconds + 120)
    after = states(api, ids)
    released = sum(1 for job_id in ids if "released" in events(api, job_id))
    return {
        "scenario": "worker-scale-down",
        "action": "5 workers, each running one sleep job; then scaled to 1 worker",
        "job_seconds": args.job_seconds,
        "states_before": before,
        "states_after": after,
        "attempts": sorted(j["attempts"] for j in jobs if j),
        "released_jobs": released,
        "seconds_to_finish": round(time.time() - stopped_at, 1),
        "passed": after == {"succeeded": 5},
    }


def worker_kill(api: Api, args: argparse.Namespace) -> dict[str, Any]:
    settle_workers(api, 2)
    ids = [api.submit("sleep", {"seconds": args.job_seconds}).json()["id"]]
    job = wait_running(api, ids, 1)[0]
    before = states(api, ids)
    killed_at = time.time()
    kill_pod(job["worker_id"])
    done = finish(api, ids, args.job_seconds + 150)[0]
    attempts = done["attempts"] if done else None
    after = states(api, ids)
    return {
        "scenario": "worker-kill",
        "action": "force-deleted the worker pod (no grace period) while it ran a sleep job",
        "job_seconds": args.job_seconds,
        "killed_pod": job["worker_id"],
        "states_before": before,
        "states_after": after,
        "attempts": attempts,
        "recovered": "recovered" in events(api, ids[0]),
        "seconds_to_finish": round(time.time() - killed_at, 1),
        "passed": after == {"succeeded": 1} and attempts == 2,
    }


SCENARIOS: dict[str, Callable[[Api, argparse.Namespace], dict[str, Any]]] = {
    "worker-scale-down": scale_down,
    "worker-kill": worker_kill,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Kill or stop pods during jobs; count lost jobs.")
    parser.add_argument("scenarios", nargs="*", choices=list(SCENARIOS), help="default: all")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--job-seconds", type=float, default=20)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "chaos")
    parser.add_argument("--keep-replicas", action="store_true", help="do not resume autoscaling")
    args = parser.parse_args()

    api = Api(args.api)
    if not wait_until(api.ready, 60):
        raise SystemExit("the API is not ready")
    totals_before = api.metrics()["totals"]
    results = []
    try:
        for name in args.scenarios or list(SCENARIOS):
            result = SCENARIOS[name](api, args)
            results.append(result)
            print(f"{'PASS' if result['passed'] else 'FAIL'} {name}: {json.dumps(result)}")
    finally:
        if not args.keep_replicas:
            set_replicas(WORKERS, None)
    report = {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "machine": machine(),
        "totals_before": totals_before,
        "totals_after": api.metrics()["totals"],
        "results": results,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{report['run_id']}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    api.close()
    print(f"Saved to {path}")
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
