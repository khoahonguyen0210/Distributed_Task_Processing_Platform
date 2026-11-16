# Build Notes

What was built, why it was built that way, and how each part was checked. All dates are in 2026.

## Choices

| Choice | Why | Other options |
|---|---|---|
| Build the queue directly on Redis, with no task library | The project exists to show how a queue works inside | Celery, RQ, Dramatiq (read for ideas only, see `reuse_decisions.md`) |
| One Lua script for each change of state | Redis runs a script as one step, so two workers cannot change the same job at the same moment | `MULTI`/`WATCH` transactions, which must retry on conflict and are harder to read |
| A lease with a random token for each claim | Any worker can take back a dead worker's jobs, and a slow worker cannot overwrite the new owner's result | Heartbeats alone, with no token |
| Redis sorted sets for ready, scheduled, running, and dead-letter jobs | One kind of key keeps jobs in order and finds the ones that are due or expired | A list for each priority, or Redis streams |
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

Last full local run (2026-11-16): 123 tests passed, and Ruff and mypy (strict) reported no issues.

## Security and privacy review

| Check | Result |
|---|---|
| Personal data | None collected or stored. No accounts, tracking, or requests to other sites. The tracked files hold no names, paths, emails, or keys |
| Running submitted code | Not possible. Only six built-in job types can run, and their payloads must match a strict JSON schema with no extra fields. `llm_generate` sends the prompt to the model as text and accepts only one model name |
| Requests from other sites | A `text/plain` POST from another site is rejected (422). The WebSocket checks the `Origin` header |
| DNS rebinding | Refused by the host check (400) |
| Large requests | Refused before reading (413), or with 411 if there is no length |
| Errors and logs | Validation errors leave out the submitted values. No stack traces go to clients. Logs hold no payloads |
| Network exposure | The API listens on `127.0.0.1` only. Compose does not publish Redis |
| Container | Runs as a normal user (UID 10001) |
| Package scan | Trivy 0.67.2, 2026-10-11: 0 findings in the Python packages, and 8 high-severity CVEs (44 package entries) in Debian 13 base packages, none with a fix released yet: CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 (util-linux), CVE-2025-69720 (ncurses), CVE-2026-16742 (systemd libraries), CVE-2026-54369 (acl), CVE-2026-9538 (perl). Rebuild the image when Debian releases fixes |

The API has no sign-in on purpose, because it is meant for local use. This review is a careful check by hand, not a professional audit.

## Autoscaling on Kubernetes

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
| KEDA's Metrics API scaler, reading `backlog` from the API | The backlog adds ready, running, and due delayed jobs, and the Redis Lists scaler counts only one key. (An earlier note said that scaler reads only lists. That was wrong: the 2.21 source checks the key type and counts sorted sets with `ZCARD`, although its documentation mentions only lists. Corrected on 2026-11-16) | Redis Lists scaler (does not fit), Prometheus scaler (needs a Prometheus server) |
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

## Language model jobs

This part adds the `llm_generate` job type, a model server, deadlines, a queue limit, Prometheus, and autoscaling for the model servers. Details are in `docs/inference.md` and `docs/observability.md`.

### Facts recorded before the work

| Question | Answer |
|---|---|
| Hardware | Intel i7-1260P (4 performance cores with 2 threads each, 8 efficiency cores). Docker gives 16 CPUs and 7.6 GiB. Another project's kind cluster used about 4.2 GiB and up to 1 CPU the whole time |
| Model | Qwen2.5-0.5B-Instruct, Q4_K_M GGUF, 491,400,032 bytes, SHA-256 `74a4da8c...d7a9db`, Apache-2.0. The hash matches the one Hugging Face publishes |
| Model server image | `ghcr.io/ggml-org/llama.cpp:server-b12407`, pinned by digest `sha256:89fe93c2...b392` |
| What `llamacpp:requests_deferred` counts | Requests waiting for a free slot. Test: send more requests than slots and read `/metrics` every 0.2 s. 2 slots, 6 requests: peak 2 running, 3 waiting. 4 slots, 8 requests: peak 4 waiting |
| How the context is split | `kv_unified = false` when `--parallel` is set, so `--ctx-size 4096` gives each of 2 slots 2048 |
| Prometheus install | Plain manifests, not a Helm chart, so every setting is in this repository. Prometheus used about 50 MiB |
| CI runner | A GitHub-hosted runner has a few CPUs and no model file. The CI workflow uses a fake model server, so it needs no model download |

### Choices

| Choice | Why | Other options |
|---|---|---|
| llama.cpp on CPU with a 0.5B model | Free, runs on this laptop, and reports the metrics KEDA needs | vLLM (needs a GPU), a larger model (too slow here) |
| Model file mounted from `./models` | Pods never download it, and Git never sees it | Download in an init container on each start |
| Deadline on the job, not in the payload | Every job type can use it, and the state machine needs it in one place | A field only for `llm_generate` |
| Expire in `claim.lua` | A late job is caught at the moment a worker would start it, in one Redis step | A separate sweep, which could race with the claim |
| Queue limit checked inside `enqueue.lua` | The limit can never be passed by two requests at once | A check in Python before the insert |
| Latency histogram kept in Redis | All workers add to one histogram, and the API serves it with the other metrics | One histogram per worker, merged in Prometheus |
| 4 CPUs and 4 threads per model server, 2 slots | 4 threads were about twice as fast as 2. 2 slots make waiting requests show up early | 2 CPUs per server, 4 slots |
| At most 2 model servers | Total speed stopped rising at 2 (see "Measurements") | 3 or 4, as the CPU count alone would suggest |
| 3 workers with 2 jobs each in the benchmark | 6 requests at once is a little more than 2 servers with 2 slots, so extra work waits in the Redis queue, where the queue limit can see it | 5 workers with 4 jobs each, which hid the waiting inside the model servers |

### Measurements

| What | Result |
|---|---|
| Speed of one server outside kind | 4 threads: 14.9 to 22.7 tokens/s for the whole server. 2 threads: 8.0 to 10.4 tokens/s |
| Total speed with 1, 2, and 3 servers outside kind (4 threads each) | 14.9, 20.9, and 14.8 tokens/s |
| Model load time in kind | 23 s on the first start. 24 s from container start to ready |
| Memory per server | About 170 to 190 MiB, plus the shared 491 MB model file |
| Baseline for the latency goal | First run: 180 s, 18 jobs, p95 31.1 s. With 18 jobs, p95 is the slowest job, so a second run of 300 s with 30 jobs replaced it: p50 7.0 s, p95 12.3 s, p99 16.0 s |
| Capacity in kind, 40 jobs sent at 1 per second | 1 server: 40 of 40 succeeded, 0.287 jobs/s, 17.4 tokens/s. 3 servers: 13 succeeded, 27 expired, 22 runs retried after timeouts, 0.062 jobs/s |
| Why 3 servers were slow in kind | One server had 1.2 million major page faults: memory was short, so model pages kept being dropped and read again from the Windows folder mount. Linux reported 25% memory pressure and 81% CPU pressure on the node |
| CPU speed | Windows reported the CPU at 44% to 76% of its normal speed during this work. One run of the same test was 3 times slower than another |
| LLM benchmark (5 modes, 2 trials each, 240 jobs) | 239 succeeded, 1 refused with 429, 0 expired, 0 lost, 0 retried. Median p95: fixed 1 server 10.1 s, fixed 2 servers 22.1 s, KEDA 21.0 s. KEDA used 345 server-seconds against 480 for 2 fixed servers. Raw data in `benchmarks/inference/20261116T161227Z/` |
| Why the LLM trials varied | The average run time per job ranged from 2.6 s to 29.2 s. The slowest trial had 36% memory pressure and about 645 major page faults per second. One model server had read 631,061 file pages again (about 2.4 GB). Its pod never reached the 768 MiB limit, so the cause was the whole Docker machine running short of memory, with 4.3 GiB used by the other project's cluster |

### Problems found and fixed

| Problem | How it was found | Fix |
|---|---|---|
| A worker that starts before Redis is ready exits, and Kubernetes waits longer and longer before each restart | The first deploy of the inference stack | The worker now waits and tries Redis again every 2 s at start. A test covers it |
| KEDA warned that `pollingInterval` and `cooldownPeriod` do nothing when the minimum is 1 | The KEDA warning on `kubectl apply` | Both were removed from the model-server scaler |
| The benchmark first sent 20 requests at once to at most 6 slots, so the waiting happened inside the model servers and the queue limit never applied | Working out the queue limit | 3 workers with 2 jobs each |
| One benchmark run started with a wrong note about the CPU speed | Reading the note after the start | It was stopped within seconds, its empty folder deleted, and the run started again with the measured range |
| The first full LLM benchmark stopped during the 4th of its 10 runs, because Docker Desktop had stopped | The output files ended in the middle of a run | The partial data was not kept, and the benchmark was started again |
| In the second attempt, the laptop went into Modern Standby twice, even though it was told to stay awake. After it woke up, one model server wrote each 64-token answer in about 1.3 s, while a run before the standby made only 13.3 tokens per second in total. Runs at such different CPU speeds cannot be compared | The benchmark's gap check, then the Windows event log (Kernel-Power events 506 and 507) | The run was stopped and its data was not kept. The third attempt also kept the display on, which stops Modern Standby, and logged the CPU speed every 10 s |
| The third attempt stopped during its 3rd run, because the laptop went to sleep at 00:44 (Kernel-Power event 42, after a program asked Windows to sleep). Keeping the display on does not stop a sleep that is asked for | The output files ended in the middle of a run, then the Windows event log | The partial data was not kept. The fourth attempt ran in daytime with the laptop on power and watched |
| The fourth attempt crashed in its 2nd run. Docker had just started both kind clusters, and the other project's Java pods kept restarting while they started. That cluster used about 9 CPUs, the Docker machine's load average reached 52, and only about 80 MiB of memory was free. Health checks then failed, the model servers and the API restarted, and the benchmark could not read the API for 90 s | The benchmark's error, then `docker stats` and `/proc/loadavg` in the node | The data was not kept. The fifth attempt started only after the load average had stayed under 4 for 2 minutes, when the other cluster was back to well under 1 CPU |
| A benchmark that stopped early, or ended with the queue limit off, left the API with its own `MAX_QUEUE_DEPTH` value instead of the one in the ConfigMap | Reading the API settings before the fourth attempt | At the end, the benchmark removes its value, so the ConfigMap value applies again |
| The first run of the cluster workflow failed in the worker-kill step. The CI cluster has no KEDA, and `kubectl get scaledobject` fails there with "the server doesn't have a resource type" instead of finding nothing | The failed step on GitHub, then the same command against a local cluster without KEDA | The scripts check for the KEDA CRD first, and treat a cluster without it as having no scalers |
| The test that kills a model server sent 8 jobs at once. The queue limit refused some of them with 429, and the test crashed. It also left the model servers fixed at 2 | The first run of the test | The test sends a refused job again after 1 s and counts the refusals. It gives the model servers back to KEDA even when it fails |

## Upstream contribution

| Item | State on 2026-11-16 |
|---|---|
| Gap found | KEDA's Redis Lists scaler also counts sorted sets, sets and hashes (it checks the key type in its Lua script), but its documentation mentions only lists. The behavior dates back to v2.0. A docs pull request for it (kedacore/keda-docs#231) was closed unmerged in 2022 |
| Checked | On KEDA 2.21.0 in kind: a sorted set of 7 gave the metric 7, a set of 5 gave 5, a hash of 3 fields gave 3, a list of 9 gave 9, a missing key gave 0, and a string key logged `unsupported key type: string` |
| Next step | Open an issue in `kedacore/keda-docs` and send a pull request only after a maintainer assigns it, as KEDA's contribution rules require. Not opened yet |

## Not checked yet

- The model-server benchmark used 2 trials per mode. The trials differed more than the modes did, so more trials, or a machine with more free memory, are needed before comparing latency between modes.
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
| CI | `.github/workflows/ci.yml`, `.github/workflows/cluster.yml` | `ci.yml` passed on a push to `inference-platform` on 2026-11-16 in 51 s: <https://github.com/khoahonguyen0210/Distributed_Task_Processing_Platform/actions/runs/43922341634>. `cluster.yml` (kind, fake model server, smoke test, worker-kill and LLM worker-kill tests) passed on a push to `main` on 2026-11-16 in 3 min 0 s, of which the kill tests took 84 s: <https://github.com/khoahonguyen0210/Distributed_Task_Processing_Platform/actions/runs/43926657625>. The plan asked for a pass on a pull request; the owner merges straight to `main`, so the workflow also runs on pushes to `main` |
| Security | README "Privacy and security", `tests/unit/test_http_limits.py`, the package scan | Checked by hand and tested. Not an audit |
| Kubernetes and KEDA | `infra/`, `docs/kubernetes.md`, `scripts/chaos_test.py`, `scripts/burst_benchmark.py` | Run on a local kind cluster |
| Language model jobs | `app/jobs/llm.py`, `infra/kubernetes/inference/`, `docs/inference.md`, `scripts/llm_benchmark.py`, `benchmarks/inference/` | Tested with a fake model server. Run, scaled up and down, and measured on a local kind cluster |
| Deadlines and the queue limit | `enqueue.lua`, `claim.lua`, `tests/integration/test_admission.py` | Tested |
| Prometheus metrics and the latency goal | `app/observability/`, `app/workers/metrics_server.py`, `docs/observability.md` | Tested, and read by Prometheus in the cluster |
| AWS, Terraform | None | Not part of this project |
