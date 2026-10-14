# What Was Reused

No code from Celery, RQ, or Dramatiq is copied into this project. They were only read, to learn how mature queues solve the same problems. Everything here is written from scratch.

| Project | What was read | What it taught | License | What was done |
|---|---|---|---|---|
| Celery `ed354d9` | The `acks_late` and `reject_on_worker_lost` docs | How a mature queue explains that a job may run more than once | BSD-3-Clause | Read only |
| RQ `b72c366` | `StartedJobRegistry.cleanup` in `rq/registry.py` | How jobs left behind by a dead worker are retried or failed | BSD-2-Clause | Read only. The recovery code is written independently in `app/queue/lua/recover.lua` |
| RQ `b72c366` | `JobStatus` in `rq/job.py` | Which job states a queue needs | BSD-2-Clause | Read only. This project picked its own state names |
| Dramatiq `8fbdb87` | `dramatiq/brokers/redis/dispatch.lua` | How one Lua script can fetch a job and update heartbeats in a single step | LGPL-3.0-or-later | Read only. No Lua code is copied |
| Dramatiq `8fbdb87` | `compute_backoff` in `dramatiq/common.py` | The idea of doubling the wait and adding a random amount | LGPL-3.0-or-later | The idea is reused. The code and formula are different (`app/queue/backoff.py`) |
| Celery, RQ, Dramatiq | The whole library | Using it as the queue itself | See above | Not used. It would hide the parts this project wants to show |
