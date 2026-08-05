# Reuse Decisions

No code from any reference repository is copied into this project.

| Repository | Component | Intended use | License | Decision | Tests needed |
|---|---|---|---|---|---|
| Celery `ed354d9` | `acks_late` and `reject_on_worker_lost` docs | Learn how a mature queue explains at-least-once delivery | BSD-3-Clause | Reference only | None (no code used) |
| RQ `b72c366` | `StartedJobRegistry.cleanup` in `rq/registry.py` | Learn how abandoned jobs are retried or failed | BSD-2-Clause | Reference only. Recovery is written independently in `app/queue/lua/recover.lua` | Lease expiry and recovery tests |
| RQ `b72c366` | `JobStatus` in `rq/job.py` | Compare state names | BSD-2-Clause | Reference only. This project defines its own states | State machine tests |
| Dramatiq `8fbdb87` | `dramatiq/brokers/redis/dispatch.lua` | Learn how one Lua script keeps fetch and heartbeat atomic | LGPL-3.0-or-later | Reference only. No Lua code copied | Competing claim tests |
| Dramatiq `8fbdb87` | `compute_backoff` in `dramatiq/common.py` | Learn the doubling plus jitter pattern | LGPL-3.0-or-later | Reimplement independently with a different formula (`app/queue/backoff.py`) | Backoff bound tests |
| Celery, RQ, Dramatiq | Whole library as the queue engine | Run jobs | See above | Reject. It would hide the mechanics this project is meant to show | Not applicable |
