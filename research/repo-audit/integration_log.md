# Integration Log

What was built, the main decisions, and how each part was checked. Dates are in 2026.

## Decisions

| Decision | Reason | Other options considered |
|---|---|---|
| Build the queue on Redis directly, with no task library | The project exists to show the queue mechanics | Celery, RQ, Dramatiq (studied as references only, see `reuse_decisions.md`) |
| One Lua script for each change of state | Redis runs a script as one step, so claims and transitions cannot interleave | `MULTI`/`WATCH` transactions, which retry on conflict and are harder to read |
| Leases with a random token per claim | Lets any worker recover a dead worker's jobs, and stops a late worker from overwriting the new owner's result | Heartbeat-only detection with no fence |
| Sorted sets for ready, scheduled, leases, and dead jobs | One structure gives priority order, time order, and range queries for due or expired entries | Lists per priority, streams |
| Strict priority with no aging | Simple and easy to test. Starvation is documented | Aging, weighted fair queues |
| Due-job promotion and lease recovery run in every worker | No extra scheduler process to run or monitor | A separate scheduler service |
| Threads inside a worker, processes for scale | Threads suit waiting jobs; processes are needed for CPU work because of the GIL. The benchmark measures both | asyncio workers |
| Redis Pub/Sub for live events, read once by the API | Events are hints, and the dashboard reloads from the API after any gap | Redis Streams, which would keep events but add trimming and consumer state |
| Plain HTML, CSS, and JavaScript dashboard | Small, no build step, no outside files, strict Content Security Policy | React or another framework |
| `127.0.0.1` instead of `localhost` in every default | On Windows, each new connection to `localhost` waited about 2.1 s for IPv6 to fail first | None |
| No Terraform | The project runs fully on one machine and has no real cloud use. Writing undeployed cloud code would only add names to the tool list | Terraform for AWS (rejected; AWS is not used) |

## Problems found and fixed

| Problem | How it was found | Fix |
|---|---|---|
| Dashboard events were never shown: the job's `type` field overwrote the message type `job` | The WebSocket integration test hung | Renamed the field to `job_type`, and the hub now sets `type` last |
| Worker threads waited about 2 s for each new Redis connection on Windows | The first benchmark showed 3 threads running jobs one after another | Default Redis address changed to `127.0.0.1` |
| The `dead` and `jobs` indexes kept IDs of expired jobs forever, and the dead-letter count kept counting them | Review of memory growth | Workers remove expired index entries at each heartbeat; delays are capped at 1 day so the cutoff is known |

## How each part was checked

| Part | Automated check | Manual check |
|---|---|---|
| API validation and errors | `tests/integration/test_api.py`, `tests/unit/test_api_without_redis.py` | Submitted jobs with curl |
| Atomic claims | `test_competing_claims_never_share_a_job` (8 threads, 60 jobs) | None |
| Priority and delays | `test_higher_priority_first_then_fifo`, `test_delayed_job_is_not_claimed_early` | Delayed job shown as scheduled in the dashboard |
| Retries and dead letters | `test_retryable_failure_retries_then_dead_letters`, `test_dead_letters_and_replay` | `flaky` and `fail` jobs in the dashboard |
| Lease fence and recovery | `test_stale_lease_token_cannot_finish_job`, `test_killed_worker_job_is_recovered_by_another` | `docker kill -s KILL` on a worker during a 20 s job: the job was recovered and succeeded on its second try |
| Clean shutdown | `test_shutdown_waits_for_running_job`, `test_shutdown_after_grace_releases_job` | `docker stop` during a 4 s job: the job finished, then the worker exited in 2.8 s |
| Live events | `tests/integration/test_events.py`, `tests/unit/test_events.py` | Headless Chrome: jobs submitted from the form appeared live as queued and running |
| Dashboard | `tests/unit/test_dashboard.py` (headers, allowed files) | Headless Chrome screenshots in light and dark mode, no console errors |
| Docker stack | `scripts/smoke_test.py` | `docker compose up --build --wait`: all services healthy |
| Benchmark output | `tests/unit/test_benchmark.py` | Full run: 1, 3, 5 workers, both models, 3 trials, 7,200 of 7,200 jobs succeeded |

Last full local run: 90 tests passed, Ruff and mypy (strict) reported no issues.

## Not checked yet

- The GitHub Actions workflow has not run, because the repository has not been pushed. It should be checked after the first push.
- The stack was run only on Windows 11 with Docker Desktop. Linux is covered only by the CI workflow once it runs.
- Valkey, as an open source replacement for Redis 7.4, was not tested.

## Skills and evidence

| Skill | Evidence | Status |
|---|---|---|
| Python backend and API design | `app/api/`, OpenAPI at `/docs` | Tested |
| Redis data structures and atomic changes | `app/queue/lua/`, `docs/design.md` | Tested against a real Redis |
| Concurrency and worker coordination | `app/workers/worker.py`, competing-claim test, benchmark | Tested and measured |
| Retries, backoff, and recovery | `fail.lua`, `recover.lua`, `backoff.py`, worker-kill test | Tested |
| Scheduling and dead letters | `promote.lua`, `replay.lua` | Tested |
| Live updates and observability | `app/events/`, `/metrics`, JSON logs | Tested |
| Containers | `docker/Dockerfile`, `docker/docker-compose.yml` | Run locally |
| Benchmarking | `scripts/benchmark.py`, `benchmarks/results/` | Measured on one laptop |
| CI | `.github/workflows/ci.yml` | Written, not yet run |
| Security review | README "Privacy and security" | Reviewed by hand; not an audit |
| AWS, Terraform, Kubernetes | None | Not part of this project |
