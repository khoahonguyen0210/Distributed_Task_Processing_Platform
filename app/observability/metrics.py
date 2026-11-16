import math
from typing import Any

from app.queue.redis_queue import LATENCY_BUCKETS_MS, RedisQueue


def percentile(values: list[int] | list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def collect(queue: RedisQueue) -> dict[str, Any]:
    stats = queue.stats()
    workers = queue.workers()
    durations = queue.recent_durations()
    alive = sum(1 for w in workers if w["alive"])
    return {
        "queue": {
            "ready": stats["ready"],
            "scheduled": stats["scheduled"],
            "running": stats["running"],
            "dead_letter": stats["dead_letter"],
            "due": stats["due"],
        },
        "backlog": stats["ready"] + stats["running"] + stats["due"],
        "totals": {k.removeprefix("total_"): v for k, v in stats.items() if k.startswith("total_")},
        "workers": {"alive": alive, "stale": len(workers) - alive},
        "job_duration_ms": {
            "samples": len(durations),
            "mean": round(sum(durations) / len(durations), 1) if durations else None,
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
            "max": float(max(durations)) if durations else None,
        },
        "latency_ms": queue.latency(),
    }


def prometheus_text(m: dict[str, Any]) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if value is not None:
                lines.append(f"{name}{labels} {value}")

    metric(
        "taskq_queue_jobs",
        "gauge",
        "Jobs currently in each queue structure.",
        [(f'{{queue="{k}"}}', v) for k, v in m["queue"].items()],
    )
    metric(
        "taskq_backlog_jobs",
        "gauge",
        "Jobs that need a worker now: ready, running, and scheduled jobs that are due.",
        [("", m["backlog"])],
    )
    metric(
        "taskq_jobs_total",
        "counter",
        "Job events since the Redis data was created.",
        [(f'{{event="{k}"}}', v) for k, v in m["totals"].items()],
    )
    metric(
        "taskq_workers",
        "gauge",
        "Known workers by liveness.",
        [(f'{{status="{k}"}}', v) for k, v in m["workers"].items()],
    )
    d = m["job_duration_ms"]
    metric(
        "taskq_job_duration_ms",
        "summary",
        "Run time of recent successful jobs.",
        [('{quantile="0.5"}', d["p50"]), ('{quantile="0.95"}', d["p95"]), ("_count", d["samples"])],
    )
    lines.extend(latency_histogram(m.get("latency_ms", {})))
    return "\n".join(lines) + "\n"


def latency_histogram(latency: dict[str, dict[str, int]]) -> list[str]:
    name = "taskq_job_end_to_end_seconds"
    lines = [
        f"# HELP {name} Time from submission to success, for jobs that succeeded.",
        f"# TYPE {name} histogram",
    ]
    for job_type, counts in sorted(latency.items()):
        running = 0
        for edge in LATENCY_BUCKETS_MS:
            running += counts.get(str(edge), 0)
            lines.append(f'{name}_bucket{{type="{job_type}",le="{edge / 1000:g}"}} {running}')
        lines.append(f'{name}_bucket{{type="{job_type}",le="+Inf"}} {counts.get("count", 0)}')
        lines.append(f'{name}_sum{{type="{job_type}"}} {counts.get("sum", 0) / 1000:g}')
        lines.append(f'{name}_count{{type="{job_type}"}} {counts.get("count", 0)}')
    return lines
