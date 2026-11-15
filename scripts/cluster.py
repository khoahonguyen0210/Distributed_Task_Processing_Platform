import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from app.queue.models import TERMINAL_STATES

CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-taskq")
NAMESPACE = os.environ.get("KUBE_NAMESPACE", "taskq")
PAUSE_ANNOTATION = "autoscaling.keda.sh/paused-replicas"
TERMINAL = {state.value for state in TERMINAL_STATES}
FINISHED_EVENTS = ("succeeded", "failed", "dead_letter", "cancelled", "expired")


def kubectl(*args: str, check: bool = True, timeout: float = 60) -> str:
    command = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def has_scaler(name: str) -> bool:
    return bool(kubectl("get", "scaledobject", name, "--ignore-not-found", "-o", "name"))


def deployment_replicas(name: str) -> tuple[int, int]:
    out = kubectl(
        "get", "deploy", name, "-o", "jsonpath={.status.replicas},{.status.readyReplicas}"
    )
    total, ready = (out.split(",") + ["", ""])[:2]
    return int(total or 0), int(ready or 0)


def set_replicas(deployment: str, count: int | None) -> None:
    if has_scaler(deployment):
        if count is None:
            kubectl("annotate", "scaledobject", deployment, f"{PAUSE_ANNOTATION}-")
        else:
            kubectl(
                "annotate", "--overwrite", "scaledobject", deployment, f"{PAUSE_ANNOTATION}={count}"
            )
    elif count is not None:
        kubectl("scale", "deploy", deployment, f"--replicas={count}")


def wait_until(check: Callable[[], bool], timeout: float, every: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(every)
    return False


def pod_names(app: str) -> list[str]:
    out = kubectl("get", "pods", "-l", f"app={app}", "-o", "jsonpath={.items[*].metadata.name}")
    return out.split()


def kill_pod(name: str) -> None:
    kubectl("delete", "pod", name, "--grace-period=0", "--force", "--wait=false")


class Api:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=15)

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(6):
            try:
                return self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 5:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    def submit(self, job_type: str, payload: dict[str, Any], **options: Any) -> httpx.Response:
        body = {"type": job_type, "payload": payload, **options}
        return self.request("POST", "/api/jobs", json=body)

    def submit_many(
        self, count: int, job_type: str, payload: dict[str, Any], **options: Any
    ) -> list[httpx.Response]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(lambda _: self.submit(job_type, payload, **options), range(count)))

    def job(self, job_id: str) -> dict[str, Any] | None:
        response = self.request("GET", f"/api/jobs/{job_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def jobs(self, ids: list[str]) -> list[dict[str, Any] | None]:
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(self.job, ids))

    def metrics(self) -> dict[str, Any]:
        data: dict[str, Any] = self.request("GET", "/api/metrics").json()
        return data

    def finished_total(self) -> int:
        totals = self.metrics()["totals"]
        return sum(int(totals.get(name, 0)) for name in FINISHED_EVENTS)

    def ready(self) -> bool:
        try:
            return self.http.get("/health/ready").status_code == 200
        except httpx.HTTPError:
            return False

    def alive_workers(self) -> int:
        return int(self.metrics()["workers"]["alive"])

    def unfinished(self, ids: list[str]) -> list[str]:
        return [
            job_id
            for job_id, job in zip(ids, self.jobs(ids), strict=True)
            if job is not None and job["state"] not in TERMINAL
        ]


def cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def machine() -> dict[str, Any]:
    def run(*command: str) -> str:
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    docker = run("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}").split()
    node = json.loads(run("kubectl", "--context", CONTEXT, "get", "nodes", "-o", "json") or "{}")
    nodes = node.get("items", [])
    keda = run(
        "kubectl",
        "--context",
        CONTEXT,
        "-n",
        "keda",
        "get",
        "deploy",
        "keda-operator",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )
    return {
        "cpu": cpu_name(),
        "logical_cpus": os.cpu_count(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "docker_cpus": int(docker[0]) if docker else None,
        "docker_memory_gib": round(int(docker[1]) / 2**30, 2) if len(docker) > 1 else None,
        "kubernetes": nodes[0]["status"]["nodeInfo"]["kubeletVersion"] if nodes else None,
        "nodes": len(nodes),
        "keda_image": keda or None,
    }
