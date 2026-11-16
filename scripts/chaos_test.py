import argparse
import json
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.cluster import (
    TERMINAL,
    Api,
    deployment_replicas,
    kill_pod,
    machine,
    pod_names,
    set_replicas,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent
WORKERS = "worker"
MODEL_SERVERS = "model-server"
LONG_ANSWER = "Count from 1 to 400, one number per line."


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


def llm_worker_kill(api: Api, args: argparse.Namespace) -> dict[str, Any]:
    settle_workers(api, 2)
    payload = {"prompt": LONG_ANSWER, "max_tokens": args.llm_tokens}
    ids = [api.submit("llm_generate", payload).json()["id"]]
    job = wait_running(api, ids, 1)[0]
    before = states(api, ids)
    killed_at = time.time()
    kill_pod(job["worker_id"])
    done = finish(api, ids, 300)[0]
    attempts = done["attempts"] if done else None
    after = states(api, ids)
    return {
        "scenario": "llm-worker-kill",
        "action": "force-deleted the worker pod while it waited for the model server",
        "max_tokens": args.llm_tokens,
        "killed_pod": job["worker_id"],
        "states_before": before,
        "states_after": after,
        "attempts": attempts,
        "recovered": "recovered" in events(api, ids[0]),
        "seconds_to_finish": round(time.time() - killed_at, 1),
        "passed": after == {"succeeded": 1} and attempts == 2,
    }


def submit_accepted(
    api: Api, count: int, job_type: str, payload: dict[str, Any], timeout: float = 300
) -> tuple[list[str], int]:
    ids: list[str] = []
    refused = 0
    deadline = time.monotonic() + timeout
    while len(ids) < count:
        response = api.submit(job_type, payload)
        if response.status_code == 202:
            ids.append(response.json()["id"])
            continue
        if response.status_code != 429 or time.monotonic() > deadline:
            raise RuntimeError(f"unexpected answer {response.status_code}: {response.text}")
        refused += 1
        time.sleep(1)
    return ids, refused


def model_server_kill(api: Api, args: argparse.Namespace) -> dict[str, Any]:
    settle_workers(api, 2)
    set_replicas(MODEL_SERVERS, 2)
    try:
        if not wait_until(lambda: deployment_replicas(MODEL_SERVERS) == (2, 2), 300, 2):
            raise TimeoutError("2 model servers did not start")
        payload = {"prompt": LONG_ANSWER, "max_tokens": args.llm_tokens}
        ids, refused = submit_accepted(api, args.llm_jobs, "llm_generate", payload)
        wait_running(api, ids, 2)
        before = states(api, ids)
        victim = pod_names(MODEL_SERVERS)[0]
        killed_at = time.time()
        kill_pod(victim)
        jobs = finish(api, ids, 600)
        after = states(api, ids)
    finally:
        set_replicas(MODEL_SERVERS, None)
    lost = sum(n for state, n in after.items() if state not in TERMINAL)
    return {
        "scenario": "model-server-kill",
        "action": "force-deleted 1 of 2 model-server pods while requests were running",
        "jobs": len(ids),
        "refused_then_sent_again": refused,
        "max_tokens": args.llm_tokens,
        "killed_pod": victim,
        "states_before": before,
        "states_after": after,
        "retried_runs": sum(max(0, j["attempts"] - 1) for j in jobs if j),
        "lost": lost,
        "seconds_to_finish": round(time.time() - killed_at, 1),
        "passed": lost == 0,
    }


SCENARIOS: dict[str, Callable[[Api, argparse.Namespace], dict[str, Any]]] = {
    "worker-scale-down": scale_down,
    "worker-kill": worker_kill,
    "llm-worker-kill": llm_worker_kill,
    "model-server-kill": model_server_kill,
}
DEFAULT = ["worker-scale-down", "worker-kill"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Kill or stop pods during jobs; count lost jobs.")
    parser.add_argument(
        "scenarios", nargs="*", choices=list(SCENARIOS), help="default: the two worker tests"
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--job-seconds", type=float, default=20)
    parser.add_argument("--llm-tokens", type=int, default=256)
    parser.add_argument("--llm-jobs", type=int, default=8)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "chaos")
    parser.add_argument("--keep-replicas", action="store_true", help="do not resume autoscaling")
    args = parser.parse_args()

    api = Api(args.api)
    if not wait_until(api.ready, 60):
        raise SystemExit("the API is not ready")
    totals_before = api.metrics()["totals"]
    results = []
    try:
        for name in args.scenarios or DEFAULT:
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
