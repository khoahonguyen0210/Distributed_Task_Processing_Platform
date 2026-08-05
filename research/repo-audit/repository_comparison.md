# Repository Comparison

Three mature Python task queues were studied to learn how they handle job states, retries, scheduling, and lost workers. Their code was read but not run.

## Clones

Each was cloned with `git clone --depth 1` into `research/reference_repos/` (ignored by Git) on 2026-10-10.

| Repository | URL | Branch | Commit | Commit date |
|---|---|---|---|---|
| Celery | https://github.com/celery/celery | main | `ed354d9100dbdaf7b867f103593b4dea4148670a` | 2026-10-10 |
| RQ | https://github.com/rq/rq | master | `b72c366e53dc945a8891caa4c3f01c4af8d039a5` | 2026-10-10 |
| Dramatiq | https://github.com/Bogdanp/dramatiq | master | `8fbdb875fc9ae9e810a45c3c60644227c23c2bbf` | 2026-10-05 |

## What each one does

| Topic | Celery | RQ | Dramatiq |
|---|---|---|---|
| Files read | `docs/userguide/tasks.rst` (`acks_late`, `reject_on_worker_lost`) | `rq/job.py` (`JobStatus`), `rq/registry.py` (`StartedJobRegistry.cleanup`), `rq/queue.py` (`lmove`), `rq/worker/base.py` (`worker_ttl`) | `dramatiq/brokers/redis/dispatch.lua`, `dramatiq/brokers/redis.py`, `dramatiq/common.py` (`compute_backoff`), `dramatiq/middleware/retries.py` |
| Job states | Pending, started, success, failure, retry, revoked | `queued`, `started`, `finished`, `failed`, `scheduled`, `deferred`, `stopped`, `canceled`, and more | Messages are acked or moved to a dead-letter queue |
| Claiming work | Done by the broker (Kombu) | `BLMOVE` from the queue list into a per-worker "intermediate" list | One Lua script does fetch, ack, and heartbeat updates |
| Lost workers | With `acks_late`, the broker sends the message again | Jobs in the "started" registry past their expiry are retried or failed by `cleanup()` | Heartbeats in a sorted set; messages of dead workers are re-queued during maintenance |
| Retries | Per task, with countdown and optional jitter | `Retry(max, interval)` object | Middleware with exponential backoff and jitter, capped by `max_backoff` |
| Delivery | At least once with `acks_late`; docs say tasks must be idempotent | At least once for abandoned jobs that still have retries | At least once |
| Priority | Depends on the broker | Separate queues, read in order | Separate queues |

## Lessons used in this project

1. **A lease with an expiry is the key to recovery.** RQ and Dramatiq both keep a time-sorted record of jobs in progress and move expired ones back. This project does the same with a `leases` sorted set.
2. **Claiming must be one atomic step.** Dramatiq does it in one Lua script. This project uses Lua scripts for every state change.
3. **Backoff needs a cap and jitter.** Dramatiq doubles the delay each attempt, adds random jitter, and caps it. This project uses the same idea with its own formula.
4. **Duplicate runs are possible.** All three accept that a job may run twice after a crash. This project documents at-least-once delivery and keeps its handlers idempotent.
5. **Lost leases need a fence.** A worker whose lease expired must not overwrite the new owner's result. This project gives each claim a random lease token and checks it on every write.

## Why none of them is used as the engine

The goal of this project is to show the queue mechanics directly: data structures, atomic claims, leases, retries, and recovery. Using one of these libraries would hide that work. All three are **reference only**.
