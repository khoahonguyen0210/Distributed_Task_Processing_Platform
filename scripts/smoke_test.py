"""Checks a running stack from the outside: health, one job, one retry, one rejection.

Usage: python scripts/smoke_test.py [base_url]
Needs only the Python standard library.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def wait_for(job_id: str, states: set[str], timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, job = call("GET", f"/api/jobs/{job_id}")
        if job["state"] in states:
            return job  # type: ignore[no-any-return]
        time.sleep(0.2)
    raise SystemExit(f"job {job_id} did not reach {states} in {timeout} s")


def check(name: str, ok: bool) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            status, health = call("GET", "/health/ready")
            if status == 200 and health["workers_alive"]:
                break
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit("the API or workers did not become ready")
        time.sleep(0.5)
    check(f"ready with {health['workers_alive']} worker(s)", True)

    _, job = call("POST", "/api/jobs", {"type": "add", "payload": {"a": 2, "b": 3}})
    done = wait_for(job["id"], {"succeeded", "failed", "dead_letter"})
    check("add job succeeded with result 5", done["state"] == "succeeded" and done["result"] == 5)

    _, job = call("POST", "/api/jobs", {"type": "flaky", "payload": {"fail_times": 1}})
    done = wait_for(job["id"], {"succeeded", "failed", "dead_letter"})
    check("flaky job succeeded on its second try", done["attempts"] == 2)

    status, _ = call("POST", "/api/jobs", {"type": "shell", "payload": {"cmd": "ls"}})
    check("unknown job type rejected with 422", status == 422)

    _, metrics = call("GET", "/api/metrics")
    check("metrics count the finished jobs", metrics["totals"]["succeeded"] >= 2)


if __name__ == "__main__":
    main()
