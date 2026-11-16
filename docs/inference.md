# Language Model Jobs

This page connects a small language model to the queue. A job of type `llm_generate` asks the model for text. The model runs in its own pods, called model servers, and KEDA adds or removes them as the number of requests changes. Everything runs on one laptop, with no cloud and no paid service.

## What runs

| Part | Count | Size of each pod | Job |
|---|---|---|---|
| Model server (llama.cpp `llama-server`, build 12407) | 1 to 2 | 4 CPUs, 768 MiB | Runs the model. 2 requests at a time |
| Prometheus 3.16.0 | 1 | Up to 1 CPU, 512 MiB | Reads metrics for KEDA and for the benchmark |
| Workers | 0 to 5 | Up to 1 CPU, 128 MiB | Run jobs, 2 at a time, and call the model server |
| API and Redis | 1 each | Up to 1 CPU, 256 MiB | As in [`kubernetes.md`](kubernetes.md) |

The model servers, Prometheus, and Redis have cluster-internal addresses only. A network policy lets only workers and Prometheus reach the model servers.

## The model

| Item | Value |
|---|---|
| Model | Qwen2.5-0.5B-Instruct, by the Qwen team |
| File | `qwen2.5-0.5b-instruct-q4_k_m.gguf` (GGUF format, Q4_K_M, 491,400,032 bytes) |
| SHA-256 | `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db` |
| Source | Hugging Face repository `Qwen/Qwen2.5-0.5B-Instruct-GGUF`, revision `9217f5db79a29953eb74d5343926648285ec7e67` |
| License | Apache-2.0 |

The model file is not in this repository. Download it into `models/`, which Git ignores:

```bash
mkdir -p models
curl -L -o models/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf
sha256sum models/qwen2.5-0.5b-instruct-q4_k_m.gguf
```

## Start it

Run these in the repository folder, after the model download. `infra/kind/cluster.yaml` mounts `./models` into the node as a read-only folder, so pods never download the model.

```bash
kind create cluster --config infra/kind/cluster.yaml
docker build -f docker/Dockerfile -t taskq:local .
docker save --platform linux/amd64 -o taskq.tar taskq:local
kind load image-archive taskq.tar --name taskq
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --version 2.21.0 --namespace keda --create-namespace --wait
kubectl apply -k infra/kubernetes/inference
kubectl -n taskq port-forward svc/api 8000:8000
```

Then submit a job:

```bash
curl -X POST http://127.0.0.1:8000/api/jobs -H "Content-Type: application/json" \
  -d '{"type": "llm_generate", "payload": {"prompt": "Explain what a job queue is."}}'
```

## The `llm_generate` job

| Payload field | Allowed values |
|---|---|
| `model` | Only `qwen2.5-0.5b-instruct` (the default) |
| `prompt` | 1 to 2,000 characters |
| `max_tokens` | 1 to 256, default 64 |

The temperature is always 0, and the worker never runs anything from the prompt. The result holds the text (at most 2,000 characters), whether it was cut, and the token counts.

| What the worker sees | What happens |
|---|---|
| No answer in 120 s, or no connection | The job is tried again, with the normal waits between tries |
| Status 408, 429, 500, 502, 503, or 504 | The job is tried again |
| Any other 4xx status | The job fails at once. Trying again would get the same answer |
| An answer that cannot be read | The job is tried again |

Each `llm_generate` job has a deadline of 300 s, unless the request sets `deadline_seconds`. If no worker has started the job by then, it goes to the `expired` state and never reaches the model server. Late answers are often useless, and running them would only delay the jobs that wait after them.

## Queue limit

When the ready queue holds `MAX_QUEUE_DEPTH` jobs, the API refuses new jobs with `429 Too Many Requests` and a `Retry-After` header. The check and the insert are one Redis step. The Kubernetes setup uses `MAX_QUEUE_DEPTH=3`, worked out from measurements:

```text
2 servers finished 0.327 jobs per second (measured outside kind, 64 tokens each)
A new job waits for (Q + 2) jobs ahead of it: Q in the queue, 2 waiting inside the servers
It then runs for about 7 s (p50 of the baseline)
(Q + 2) / 0.327 + 7 <= 25 s (the latency goal)   =>   Q <= 3.9   =>   Q = 3
```

Docker Compose leaves the limit off (`0`).

## Autoscaling the model servers

KEDA turns two Prometheus queries into scaling targets. Kubernetes reads them about every 15 seconds and follows the one that asks for more servers.

| Trigger | Query | Target per server | Why |
|---|---|---|---|
| Waiting requests | `sum(llamacpp:requests_deferred) or vector(0)` | 2 | Each waiting request adds about 3.5 s (7 s per job, 2 slots). 2 waiting requests keep a new job near 12.3 + 7 = 19 s, under the 25 s goal |
| p95 end-to-end time | The p95 query in [`observability.md`](observability.md) | 25 s | The latency goal. If p95 is twice the goal, KEDA asks for twice the servers |

| Setting | Value | Why |
|---|---|---|
| `minReplicaCount` | 1 | Loading the model took 23 s on a first start, too slow for a first request |
| `maxReplicaCount` | 2 | See "How many model servers fit" below |
| Scale up | At once | A burst needs servers quickly |
| Scale down | After 120 s below both targets | Avoids stopping a server that the next burst needs again |

Workers keep their own scaler from [`kubernetes.md`](kubernetes.md), with a target of 2 jobs per worker, because each worker now runs 2 jobs at once. A worker waits up to 130 s for a running job when it is removed, since a model request can take 120 s.

## What was measured first

| Question | Answer |
|---|---|
| What does `llamacpp:requests_deferred` count? | Requests the server has received that are waiting for a free slot. With 2 slots and 6 requests at once, `requests_processing` peaked at 2 and `requests_deferred` at 3. With 4 slots and 8 requests, `requests_deferred` peaked at 4. Near the start of a burst it can count fewer, before the server has read every request |
| How is the context split across slots? | With `--parallel` set, this build turns the shared cache off (`kv_unified = false`) and splits `--ctx-size` evenly: 4096 with 2 slots gives 2048 per slot |
| How fast is one server? | 4 threads: about 15 to 23 tokens/s for the whole server. 2 threads: about 8 to 10 tokens/s. A 64-token answer took 7 s (p50) |
| How long does a server take to start? | 23 s to load the model on the first start, 24 s from container start to ready. The first image pull took 1 min 45 s |
| How much memory? | About 170 to 190 MiB per server, plus the 491 MB model file in the page cache, which all servers on the node share. Prometheus used about 50 MiB |

## How many model servers fit

The plain rule is cores ÷ CPUs per server: 16 ÷ 4 = 4. The other pods ask for about 3.2 CPUs, which leaves room for 3. But the measured total speed stopped rising at 2 servers:

| Model servers (outside kind, 4 threads each) | Total tokens/s |
|---|---:|
| 1 | 14.9 |
| 2 | 20.9 |
| 3 | 14.8 |

The i7-1260P has only 4 performance cores; the rest are smaller efficiency cores. Text generation also reads the whole model from memory for each token, so servers compete for memory speed. Inside kind, 3 servers did worse still. Memory was short, so parts of the model file kept being dropped from memory. One server had 1.2 million page faults, each one a new read from the Windows folder, and total speed fell to 3.7 tokens/s. So the limit is 2 servers on this laptop. That is the point where adding servers stops helping, and it is far below what a cloud cluster could do.

## Benchmark

[`scripts/llm_benchmark.py`](../scripts/llm_benchmark.py) sends one burst of `llm_generate` jobs and compares a fixed number of model servers with KEDA. For the fixed counts, it pauses KEDA at that count. The `-open` runs turn the queue limit off.

```bash
python -m scripts.llm_benchmark
```

Measured on 2026-11-16 on one laptop (Intel i7-1260P, 16 CPUs and 7.6 GiB given to Docker), with another kind cluster from a different project running on the same Docker engine.

| Workload | Value |
|---|---|
| Shape | 15 s with no jobs, then 0.4 jobs per second for 60 s (24 jobs), measured for 240 s |
| Each job | One of 12 fixed prompts, at most 64 tokens (61 on average), deadline 60 s |
| Workers | 3 pods, 2 jobs at a time each, so at most 6 requests at once |
| Queue limit | 3 when on |
| Trials | 2 of each mode. All 5 modes ran once, then all 5 ran again |

Each number is the median of the 2 trials, which is their average:

| Model servers | Sent | Refused (429) | Succeeded | Expired or lost | p50 | p95 | Server-seconds | Second server ready |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Fixed 1 | 48 | 0 | 48 | 0 | 7.5 s | **10.1 s** | 240 | - |
| Fixed 2 | 48 | 0 | 48 | 0 | 9.2 s | 22.1 s | 480 | - |
| KEDA, 1 to 2 | 48 | 1 | 47 | 0 | 12.4 s | 21.0 s | 345 | 47.5 s (trial 1 only) |
| Fixed 2, no queue limit | 48 | 0 | 48 | 0 | 35.1 s | 42.0 s | 479 | - |
| KEDA, no queue limit | 48 | 0 | 48 | 0 | 8.3 s | 15.9 s | 338 | 62.7 s (trial 1 only) |

- **No job was lost.** 240 jobs were sent: 1 was refused with `429`, and the other 239 succeeded on their first try. None expired, failed, or went to the dead-letter list.
- **A second model server did not make jobs faster on this laptop.** Fixed 2 had a higher p95 than fixed 1 in both trials (27.8 s against 15.9 s, and 16.3 s against 4.4 s). This matches "How many model servers fit" above.
- **KEDA used about 28% less server time than 2 fixed servers** (345 and 338 server-seconds against 480). In trial 1 of both KEDA modes, it asked for a second server 29 s and 44 s into the burst, and the server was ready 18 s and 19 s later, after loading the model. That is near the end of a 60 s burst. In trial 2, one server was fast enough, and KEDA added none.
- **The queue limit was rarely reached.** With at most 6 requests at once, only 1 of 240 jobs found the queue full. With the limit off, the slowest trial of the whole run (fixed 2, trial 2) had jobs waiting 57 s in the queue (p95), close to their 60 s deadline. With the limit on, the queue cannot grow past 3 jobs, so a client would have been told at once to try again later.

### Why the trials differ so much

The same mode gave very different times in its two trials. The average time a model server took for one job ranged from 2.6 s to 29.2 s:

| Mode | Trial | p95 | Average run time per job | CPU speed (Windows) | Memory pressure (peak) | Model pages read again per second |
|---|---:|---:|---:|---:|---:|---:|
| Fixed 1 | 1 | 15.9 s | 12.7 s | 43% | not logged | not logged |
| Fixed 2 | 1 | 27.8 s | 16.3 s | 40% | not logged | not logged |
| KEDA | 1 | 29.2 s | 17.2 s | 42% | not logged | not logged |
| Fixed 2, no limit | 1 | 7.1 s | 3.5 s | 57% | not logged | not logged |
| KEDA, no limit | 1 | 27.3 s | 14.8 s | 41% | not logged | not logged |
| Fixed 1 | 2 | 4.4 s | 2.6 s | 57% | not logged | not logged |
| Fixed 2 | 2 | 16.3 s | 7.4 s | 53% | 7.1% | 764 |
| KEDA | 2 | 12.7 s | 7.0 s | 43% | 0.1% | 41 |
| Fixed 2, no limit | 2 | 76.8 s | 29.2 s | 40% | 36.0% | 645 |
| KEDA, no limit | 2 | 4.4 s | 3.0 s | 59% | 1.5% | 98 |

The node logs started during trial 2, so trial 1 has no memory numbers. There are two likely causes:

1. **Memory.** Docker had 7.6 GiB, and the other project's cluster used about 4.3 GiB of it. The model file is read through the page cache, so when memory is low, Linux drops model pages and later reads them again from the Windows folder mount. The slowest trial had 36% memory pressure and about 645 major page faults per second. One model server's counters showed 631,061 file pages read again, about 2.4 GB, or the whole model about 5 times. The pod itself never reached its 768 MiB limit.
2. **CPU speed.** Windows reported the CPU at 40% to 59% of its normal speed while jobs ran, most likely because of heat or power limits. The three fastest trials ran at 57% to 59%, and the five slowest at 40% to 43%. But a busy CPU also runs slower, so these numbers alone do not show which is the cause.

So the latency columns describe this laptop on this day more than the design. The counts (sent, refused, succeeded, lost), the server-seconds, and the scaling times are the reliable results. Raw data, including the CPU speed and node logs: [`benchmarks/inference/20261116T161227Z/`](../benchmarks/inference/20261116T161227Z/).

### Scaling up and back down

The 240 s window above ends before KEDA removes the second server. A separate run used a heavier burst, so that KEDA had to add a server, and a longer window:

```bash
python -m scripts.llm_benchmark --modes auto --trials 2 --rate 1.0 --duration 60 --window 480 --out benchmarks/inference/scaling
```

The burst was 1 job per second for 60 s, with the queue limit on. Times are counted from the first job of the burst:

| Trial | Sent | Refused (429) | Succeeded | Lost | KEDA asks for a 2nd server | 2nd server ready | Last job done | Back to 1 server |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 60 | 38 | 22 | 0 | 12.6 s | 59.0 s | 144 s | 318 s |
| 2 | 60 | 9 | 51 | 0 | 10.2 s | 17.2 s | 68 s | 176 s |

- KEDA asked for the second server 10 to 13 s after the burst began.
- The new server took 7 s to become ready in trial 2 and 46 s in trial 1. In trial 1, memory was short, as described above, and the model pages had to be read from the Windows folder.
- The second server stopped 108 s and 174 s after the last job finished. The 120 s scale-down window starts only after both triggers are under their targets, and the p95 trigger looks back 1 minute. In trial 1, p95 was far above the 25 s goal, so it stayed high for that extra minute.
- Trial 1 refused far more jobs, because its single server was slow and the queue stayed full.

Raw data: [`benchmarks/inference/scaling/20261116T170521Z/`](../benchmarks/inference/scaling/20261116T170521Z/). In trial 2, the benchmark saw a 22 s gap in its samples, while `kubectl` waited for the cluster, so it ran that trial again, as it is written to do.

## Killing pods during jobs

Both tests ran on the inference setup with `python -m scripts.chaos_test llm-worker-kill model-server-kill`. Each job asked for a 256-token answer. Raw data: [`benchmarks/chaos/20261116T052811Z.json`](../benchmarks/chaos/20261116T052811Z.json).

| Test | What was done | Result |
|---|---|---|
| Worker killed during a request | The worker pod was deleted with no grace period while its job waited for the model server | The lease ran out, another worker took the job, and it succeeded on its second try, 52.6 s after the kill |
| Model server killed during requests | 8 jobs on 2 model servers. One server was deleted with no grace period while 4 requests were running | All 8 jobs succeeded and none were lost. 2 runs were tried again, which matches the 2 requests the deleted server was running |

While the second test sent its 8 jobs, the queue limit refused 38 of the attempts with `429`. The test waited 1 s after each refusal and sent the job again, as a client should.

## Limits

- One laptop. Pods share the same cores, and other programs ran at the same time.
- Windows reported the CPU at 40% to 76% of its normal speed during this work, most likely because of heat or power limits. The speed of the same test differed by up to 5 times at different times, so compare only numbers from the same run.
- Docker had 7.6 GiB of memory, and another project's cluster used about 4.3 GiB of it. Under load, Linux kept dropping model pages and reading them again. Locking the model in memory (`--mlock`) or giving Docker more memory may make the times steadier. Neither was tried here.
- The model is very small, so its answers are short and simple. This project is about the platform around it, not the quality of the text.
