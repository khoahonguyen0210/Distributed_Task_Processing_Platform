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

## Autoscaling on Kubernetes (2026-11-15)

This part adds a local Kubernetes cluster, where KEDA starts and stops workers based on the queue. The queue itself did not change.

### Facts recorded before the work

| Question | Answer |
|---|---|
| How are waiting, delayed, running, and dead jobs stored? | Four Redis sorted sets: `ready` (by priority, then order), `scheduled` (by due time), `leases` (by lease end), and `dead` (by time of death) |
| How was queue size shown? | `/api/metrics` (JSON) and `/metrics` (Prometheus text) gave the size of each set. No Prometheus server ran |
| What is the longest job? | `sleep` allows 30 s. `cpu` at its limit (5,000,000 steps) took 0.44 s on this laptop |
| What does Docker give the cluster? | 16 CPUs and 7.6 GiB of memory. A kind cluster from another project used about 4.3 GiB of that the whole time |

### Choices

| Choice | Why | Other options |
|---|---|---|
| kind with one node | Free, runs in Docker, and is easy to delete | Docker Desktop's own Kubernetes, minikube |
| KEDA's Metrics API scaler, reading `backlog` from the API | The ready queue is a sorted set, and the Redis Lists scaler only reads the length of a list | Redis Lists scaler (does not fit), Prometheus scaler (needs a Prometheus server) |
| Count ready, running, and due delayed jobs | Workers also take back expired leases and move due jobs. With zero workers, nobody else would | Ready jobs only |
| 0 to 5 workers, 1 job per worker | Matches the earlier 1, 3, and 5 worker benchmark | Higher limits, which one laptop cannot use well |
| 35 s shutdown wait, 45 s grace period | The longest job is 30 s, so a removed worker can finish it | Shorter waits that cut jobs off |
| `restricted` Pod Security and a network policy for Redis | Containers get no extra rights, and only the API and workers can reach Redis | No limits |

### Problems found and fixed

| Problem | How it was found | Fix |
|---|---|---|
| `kind load docker-image` failed with `content digest ... not found` | First image load | `docker save --platform linux/amd64`, then `kind load image-archive` |
| Kubernetes health probes send the pod IP as the host name, which the API refuses | Reading the host check before writing the probes | The probes send `Host: localhost`. The cluster name of the API is added to `ALLOWED_HOSTS` |
| The laptop went to sleep in the middle of a check, so one poll took 26 minutes | Timestamps in the output, then the Windows event log | The benchmark measures the gap between samples and runs a trial again if a gap is over 5 s |
| `kubectl port-forward` dropped connections when the benchmark read 120 jobs every 2 s | The first benchmark run stopped with `Server disconnected` | The benchmark waits on the API's job counters and reads each job once at the end. Requests are retried |
| The benchmark summary treated a time of 0 as "no time" | A unit test | Checks now use `is not None` |
| One trial had a 14 s pause between two samples | The gap check in the benchmark | The trial was run again. All other trials had no pause |

### Checks

| Check | Result |
|---|---|
| 20 jobs sent to an empty queue | Workers went from 0 to 5 within 6 s. All 20 jobs succeeded. Workers went back to 0 after the queue was empty |
| `chaos_test.py worker-scale-down` | 5 of 5 jobs succeeded on their first try while 4 of 5 workers were stopped |
| `chaos_test.py worker-kill` | The killed worker's job was taken back and succeeded on its second try |
| Network policy | A pod in another namespace could not reach Redis. The API could |
| `burst_benchmark.py`, 3 trials each of KEDA and 1, 3, and 5 fixed workers | 1,440 of 1,440 jobs succeeded, none twice. KEDA finished a 120-job burst in 32.9 s (median) with 239 worker-seconds. 5 fixed workers took 24.1 s and 1,000 worker-seconds. Full table in `docs/kubernetes.md` |

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
| Kubernetes and KEDA | `infra/`, `docs/kubernetes.md`, `scripts/chaos_test.py`, `scripts/burst_benchmark.py` | Run on a local kind cluster |
| AWS, Terraform | None | Not part of this project |
