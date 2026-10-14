# Design

This page explains how the queue works inside: which Redis keys it uses, which script changes them, and what can go wrong. A lease is the time a worker is allowed to hold a job.

## Redis keys

Every key starts with `KEY_PREFIX` (default `taskq:`).

| Key | Type | Holds |
|---|---|---|
| `job:{id}` | Hash | One job: type, payload, state, priority, tries, times, worker, lease token, result, last error |
| `job:{id}:history` | List | The last 50 events of the job, as JSON |
| `ready` | Sorted set | Jobs that can run now. Score: priority first, then the order the job became ready |
| `seq` | String | Counter that numbers jobs as they become ready |
| `scheduled` | Sorted set | Delayed jobs and jobs waiting for a retry. Score: the time they may run |
| `leases` | Sorted set | Running jobs. Score: the time the lease runs out |
| `dead` | Sorted set | Dead-letter jobs. Score: the time they entered the list |
| `jobs` | Sorted set | Every job by creation time, for listing |
| `stats` | Hash | Counters: submitted, succeeded, failed, retried, dead_letter, cancelled, replayed, recovered |
| `durations` | List | Run time of the last 1,000 successful jobs |
| `wake` | List | One item per new ready job. Idle workers wait on it with `BLPOP` instead of polling |
| `workers` | Sorted set | Worker IDs by last heartbeat |
| `worker:{id}` | Hash | Worker details: process ID, slots, status, current jobs, counts |
| `events` | Pub/Sub channel | One message for each change of state |

A job is in exactly one of `ready`, `scheduled`, `leases`, or `dead` while it is waiting, running, or dead, and in none of them once it has finished or was cancelled.

The ready score is `(9 - priority) * 10^14 + seq`, where `seq` goes up by one each time a job becomes ready. The counter stays below 10^14, so a higher priority always comes first, and jobs with the same priority come in the order they became ready. A timestamp is not enough here, because two jobs can share a millisecond.

## Scripts

Each script runs inside Redis as one step. No other command can run in the middle of it.

| Script | Called by | Change |
|---|---|---|
| `enqueue.lua` | API | New job to `ready` (or to `scheduled` with a delay) |
| `claim.lua` | Worker | Best job from `ready` to `leases`, with a new random lease token. Skips IDs of jobs that have expired or moved on |
| `renew.lua` | Worker heartbeat | Moves the lease end forward, only if the token still matches |
| `complete.lua` | Worker | `running` to `succeeded`, only if the token matches. Sets the TTL |
| `fail.lua` | Worker | `running` to `retry_scheduled`, `failed`, or `dead_letter`, only if the token matches |
| `release.lua` | Stopping worker | `running` back to `queued`. The stopped run does not count as a try |
| `promote.lua` | Every worker, every 0.5 s | Due jobs from `scheduled` to `ready` |
| `recover.lua` | Every worker, every 0.5 s | Expired leases back to `ready`, or to `dead` if no tries are left |
| `cancel.lua` | API | `scheduled`, `queued`, or `retry_scheduled` to `cancelled` |
| `replay.lua` | API | `dead_letter` back to `queued` with the try count reset |

All scripts take the time from Redis (`TIME`), so the API, the workers, and the stored times share one clock. Each script also adds a history entry and publishes an event. The allowed changes are listed in `ALLOWED_TRANSITIONS` in [`app/queue/models.py`](../app/queue/models.py), and a test checks every published event against that table.

## Rules the scripts keep

1. **A job has at most one owner.** `claim.lua` removes the job from `ready` and creates the lease in the same step, so two workers cannot get the same job.
2. **Only the owner can finish a job.** Every claim gets a new random token, and `complete`, `fail`, `renew`, and `release` check it. If a worker's lease runs out and the job is taken back, that worker is refused and its result is dropped.
3. **A running job always has a lease.** If its worker dies, the lease runs out and `recover.lua` takes the job back. Nothing is lost silently.
4. **Tries are bounded.** A lost run counts as a try, so a job that crashes every worker ends in `dead_letter`. A run stopped by a clean shutdown does not count.
5. **Finished jobs expire.** Succeeded, failed, and cancelled jobs are deleted after `JOB_TTL_SECONDS`, dead letters after `DEAD_LETTER_TTL_SECONDS`. Workers remove expired IDs from `dead` and `jobs` every heartbeat.

## Workers

A worker is one process with a fixed number of slots (threads). Each slot claims and runs one job at a time, so a worker never takes more jobs than it can run. One more thread does the background work:

- every 5 s: heartbeat, renew the leases of running jobs, trim expired index entries
- every 0.5 s: promote due jobs, recover expired leases, forget workers silent for a long time

Every worker does the background work, so it keeps going as long as one worker is alive. The scripts are safe to run from many workers at once.

On `SIGINT`, `SIGTERM`, or Ctrl+Break, the worker stops claiming, waits up to `SHUTDOWN_GRACE_SECONDS` for running jobs, releases any job still running, and removes itself from `workers`. A second signal makes it release its jobs and exit at once.

## Duplicate runs

Delivery is at least once. A job runs twice when:

- the worker dies after the handler finished but before `complete.lua` ran, or
- the handler runs longer than the lease and the worker cannot renew it, for example because Redis was unreachable.

In the second case the first run's result is refused, so only one result is saved, but anything the first run changed outside the queue has already happened. Job code must therefore be safe to run twice (the technical word is idempotent). The built-in job types are: running one twice gives the same result and changes nothing outside the worker.

## Live events

Redis Pub/Sub keeps nothing. The API runs one event hub that reads the `events` channel and copies each message to every open WebSocket. If a browser falls behind by more than 256 messages, its old messages are dropped and it gets a `resync` message instead. If the hub loses Redis, every browser gets a `resync` after it reconnects. On `resync`, and after its own reconnect, the dashboard loads all data again from the API. The API and Redis are the source of truth; events only say that something changed.

## Durability

Docker Compose runs Redis with the append-only file (`appendfsync everysec`). A Redis crash can lose about the last second of changes. A job lost that way could be one that was accepted but never saved, or a result that was saved by the worker but not written to disk. Redis is a single server here, with no replica.
