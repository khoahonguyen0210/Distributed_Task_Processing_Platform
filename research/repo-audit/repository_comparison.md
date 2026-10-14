# How Other Queues Work

Before any code was written, three well-known Python task queues were read to see how they handle job states, retries, delayed jobs, and workers that crash. Their code was read, not run.

## What was read

Each project was cloned with `git clone --depth 1` into `research/reference_repos/` on 2026-10-10. Git ignores that folder, so none of this code is in the repository.

| Project | URL | Branch | Commit | Commit date |
|---|---|---|---|---|
| Celery | https://github.com/celery/celery | main | `ed354d9100dbdaf7b867f103593b4dea4148670a` | 2026-10-10 |
| RQ | https://github.com/rq/rq | master | `b72c366e53dc945a8891caa4c3f01c4af8d039a5` | 2026-10-10 |
| Dramatiq | https://github.com/Bogdanp/dramatiq | master | `8fbdb875fc9ae9e810a45c3c60644227c23c2bbf` | 2026-10-05 |

Files read:

- **Celery:** `docs/userguide/tasks.rst` (`acks_late`, `reject_on_worker_lost`)
- **RQ:** `rq/job.py` (`JobStatus`), `rq/registry.py` (`StartedJobRegistry.cleanup`), `rq/queue.py` (`lmove`), `rq/worker/base.py` (`worker_ttl`)
- **Dramatiq:** `dramatiq/brokers/redis/dispatch.lua`, `dramatiq/brokers/redis.py`, `dramatiq/common.py` (`compute_backoff`), `dramatiq/middleware/retries.py`

## How each one works

| Question | Celery | RQ | Dramatiq |
|---|---|---|---|
| What states can a job have? | Pending, started, success, failure, retry, revoked | `queued`, `started`, `finished`, `failed`, `scheduled`, `deferred`, `stopped`, `canceled`, and more | None. A message is either acknowledged or moved to a dead-letter queue |
| How does a worker take a job? | The message broker (Kombu) does it | `BLMOVE` moves the job from the queue into a list that belongs to that worker | One Lua script fetches the job and updates heartbeats |
| What if a worker dies? | With `acks_late`, the broker sends the message again | Jobs that stay in the "started" list past their expiry are retried or failed by `cleanup()` | Workers send heartbeats into a sorted set. During maintenance, the messages of dead workers go back in the queue |
| How are retries done? | Per task, with a countdown and an optional random delay | A `Retry(max, interval)` object | Middleware doubles the wait each time, adds a random amount, and stops at `max_backoff` |
| Can a job run twice? | Yes, with `acks_late`. The docs say tasks must be safe to repeat | Yes, for abandoned jobs that still have retries left | Yes |
| How does priority work? | It depends on the broker | Separate queues, read in order | Separate queues |

## What this project took from them

1. **Give a running job a time limit.** RQ and Dramatiq both keep running jobs in a list sorted by time, and move the expired ones back to the queue. This project does the same with a `leases` sorted set. A lease is the time a worker is allowed to hold a job.
2. **Claim a job in one step.** Dramatiq uses one Lua script for this. This project uses a Lua script for every change of state, so Redis never runs two of them at the same moment.
3. **Cap the wait before a retry, and add some randomness.** Dramatiq doubles the wait on each try, adds a random amount, and stops at a maximum. This project follows the same idea with its own formula.
4. **Accept that a job can run twice.** All three queues say a job may run again after a crash. This project says so too, and its built-in job types are safe to repeat.
5. **Stop a late worker from overwriting a newer result.** If a worker's lease runs out, another worker may take the job. Each claim therefore gets a random token, and every write must show it.

## Why none of them is used

This project exists to show how a queue works inside: the data structures, one-step claims, leases, retries, and recovery. Using one of these libraries would hide all of that, so they were used only as reading material.
