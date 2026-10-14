# Build Notes

What was built, why it was built that way, and how each part was checked. All dates are in 2026.

## Choices

| Choice | Why | Other options |
|---|---|---|
| Build the queue directly on Redis, with no task library | The project exists to show how a queue works inside | Celery, RQ, Dramatiq (read for ideas only, see `reuse_decisions.md`) |
| One Lua script for each change of state | Redis runs a script as one step, so two workers cannot change the same job at the same moment | `MULTI`/`WATCH` transactions, which must retry on conflict and are harder to read |
| A lease with a random token for each claim | Any worker can take back a dead worker's jobs, and a slow worker cannot overwrite the new owner's result | Heartbeats alone, with no token |
| Redis sorted sets for ready, scheduled, running, and dead jobs | One kind of key keeps jobs in order and finds the ones that are due or expired | A list for each priority, or Redis streams |
| High priority always goes first | Simple and easy to test. The downside, that low-priority jobs can wait for a long time, is written in the README | Raising a job's priority as it waits, or weighted queues |
| Every worker moves due jobs and takes back expired ones | There is no extra scheduler program to run or watch | A separate scheduler service |
| Threads inside a worker, and more processes to scale | Threads are fine for jobs that mostly wait. Jobs that keep the CPU busy need separate processes, because of Python's global interpreter lock. The benchmark measures both | asyncio workers |
| Redis Pub/Sub for live events, read once by the API | An event only says that something changed. After any gap, the dashboard loads everything again from the API | Redis Streams, which keep events but need trimming and tracking of each reader |
| Plain HTML, CSS, and JavaScript for the dashboard | Small, no build step, no files from other sites, and a strict Content Security Policy | React or another framework |
| `127.0.0.1` instead of `localhost` in every default | On Windows, each new connection to `localhost` waited about 2.1 s for IPv6 to fail first | None |
| No Terraform | The project runs on one machine and does not use a cloud. Cloud code that is never deployed would only pad the list of tools | Terraform for AWS (rejected, because AWS is not used) |

## Problems found and fixed

| Problem | How it was found | Fix |
|---|---|---|
| The dashboard never showed events, because the job's `type` field replaced the message type `job` | The WebSocket test hung | The field is now called `job_type`, and the hub sets `type` last |
| Worker threads waited about 2 s for each new Redis connection on Windows | In the first benchmark, 3 threads ran jobs one after another | The default Redis address is now `127.0.0.1` |
| The `dead` and `jobs` indexes kept the IDs of expired jobs forever, and the dead-letter count kept counting them | A look at memory growth | Workers remove expired entries at each heartbeat. Delays are capped at 1 day, so the cutoff is known |
| The API answered to any host name, so a website could use DNS rebinding to read it through a visitor's browser | Security review | Host names are checked against `ALLOWED_HOSTS` (default `127.0.0.1` and `localhost`) |
| Request bodies of any size were read into memory before being refused | Security review | Bodies over `MAX_BODY_BYTES` (128 KB) are refused, judged by the `Content-Length` header. Bodies without that header are refused too |

## How each part was checked

| Part | Automated test | Manual check |
|---|---|---|
| Request checks and errors | `tests/integration/test_api.py`, `tests/unit/test_api_without_redis.py` | Sent jobs with curl |
| One owner for each job | `test_competing_claims_never_share_a_job` (8 threads, 60 jobs) | None |
| Priority and delays | `test_higher_priority_first_then_fifo`, `test_delayed_job_is_not_claimed_early` | A delayed job showed as scheduled in the dashboard |
| Retries and dead letters | `test_retryable_failure_retries_then_dead_letters`, `test_dead_letters_and_replay` | `flaky` and `fail` jobs in the dashboard |
| Leases and recovery | `test_stale_lease_token_cannot_finish_job`, `test_killed_worker_job_is_recovered_by_another` | `docker kill -s KILL` on a worker during a 20 s job. The job was taken back and succeeded on its second try |
| Clean shutdown | `test_shutdown_waits_for_running_job`, `test_shutdown_after_grace_releases_job` | `docker stop` during a 4 s job. The job finished, then the worker exited after 2.8 s |
| Live events | `tests/integration/test_events.py`, `tests/unit/test_events.py` | In headless Chrome, jobs sent from the form showed up live as queued and running |
| Dashboard | `tests/unit/test_dashboard.py` (headers and allowed files) | Headless Chrome screenshots in light and dark mode, with no console errors |
| Docker stack | `scripts/smoke_test.py` | `docker compose up --build --wait`: all services healthy |
| Benchmark output | `tests/unit/test_benchmark.py` | Full run with 1, 3, and 5 workers, both models, and 3 trials. 7,200 of 7,200 jobs succeeded |

Last full local run: 94 tests passed, and Ruff and mypy (strict) reported no issues.

## Security and privacy review

| Check | Result |
|---|---|
| Personal data | None collected or stored. No accounts, tracking, or requests to other sites. The tracked files hold no names, paths, emails, or keys |
| Running submitted code | Not possible. Only five built-in job types can run, and their payloads must match a strict JSON schema with no extra fields |
| Requests from other sites | A `text/plain` POST from another site is rejected (422). The WebSocket checks the `Origin` header |
| DNS rebinding | Refused by the host check (400) |
| Large requests | Refused before reading (413), or with 411 if there is no length |
| Errors and logs | Validation errors leave out the submitted values. No stack traces go to clients. Logs hold no payloads |
| Network exposure | The API listens on `127.0.0.1` only. Compose does not publish Redis |
| Container | Runs as a normal user (UID 10001) |
| Package scan | Trivy 0.67.2, 2026-10-11: 0 findings in the Python packages, and 8 high-severity CVEs (44 package entries) in Debian 13 base packages, none with a fix released yet: CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 (util-linux), CVE-2025-69720 (ncurses), CVE-2026-16742 (systemd libraries), CVE-2026-54369 (acl), CVE-2026-9538 (perl). Rebuild the image when Debian releases fixes |

The API has no sign-in on purpose, because it is meant for local use. This review is a careful check by hand, not a professional audit.

## Not checked yet

- A full passing run of the GitHub Actions workflow has not been confirmed yet.
- The stack was run only on Windows 11 with Docker Desktop. Linux is covered only by the CI workflow.
- Valkey, a fully open source replacement for Redis 7.4, was not tested.

## Where to find each part

| Part | Where | Status |
|---|---|---|
| Python API | `app/api/`, OpenAPI page at `/docs` | Tested |
| Job storage and one-step changes in Redis | `app/queue/lua/`, `docs/design.md` | Tested against a real Redis |
| Workers and how they share jobs | `app/workers/worker.py`, the competing-claim test, the benchmark | Tested and measured |
| Retries, waits, and recovery | `fail.lua`, `recover.lua`, `backoff.py`, the worker-kill test | Tested |
| Delayed jobs and dead letters | `promote.lua`, `replay.lua` | Tested |
| Live updates and monitoring | `app/events/`, `/metrics`, JSON logs | Tested |
| Containers | `docker/Dockerfile`, `docker/docker-compose.yml` | Run locally |
| Benchmark | `scripts/benchmark.py`, `benchmarks/results/` | Measured on one laptop |
| CI | `.github/workflows/ci.yml` | Written. A full passing run is not confirmed yet |
| Security | README "Privacy and security", `tests/unit/test_http_limits.py`, the package scan | Checked by hand and tested. Not an audit |
| AWS, Terraform, Kubernetes | None | Not part of this project |
