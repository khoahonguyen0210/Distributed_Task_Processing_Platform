# Kubernetes and Autoscaling

This page runs the project on a local Kubernetes cluster. There, KEDA adds workers when jobs pile up and removes them when the queue is empty. Docker Compose is still the simplest way to run the project. This setup is for trying autoscaling. Nothing here uses a cloud.

## What you need

| Tool | Version used |
|---|---|
| Docker Desktop | 29.7.2 |
| kind | 0.33.0 (Kubernetes 1.37.0) |
| kubectl | 1.36.1 |
| Helm | 4.3.2 |
| KEDA | 2.21.0 |

## Start the cluster

Run these in the repository folder. The cluster config also mounts `models/` for the language model, and the folder may be empty.

```bash
mkdir -p models
kind create cluster --config infra/kind/cluster.yaml
docker build -f docker/Dockerfile -t taskq:local .
docker save --platform linux/amd64 -o taskq.tar taskq:local
kind load image-archive taskq.tar --name taskq
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --version 2.21.0 --namespace keda --create-namespace --wait
kubectl apply -k infra/kubernetes/autoscaling
kubectl -n taskq port-forward svc/api 8000:8000
```

Open <http://127.0.0.1:8000>. With no jobs, no worker runs. Submit a few jobs, and workers start within seconds. Remove everything with `kind delete cluster --name taskq`.

`docker save` with one platform is needed on Docker Desktop. There, `kind load docker-image` fails with `content digest ... not found`, because Docker keeps images for several platforms.

## What runs in the cluster

| Part | Kind | Reached from |
|---|---|---|
| Redis | Deployment, 1 pod, 1 GiB volume | API and worker pods only |
| API | Deployment, 1 pod | Inside the cluster, or `kubectl port-forward` |
| Workers | Deployment, 0 to 5 pods | Nothing from outside. In the inference setup, Prometheus reads their metrics port |
| KEDA scaler | `ScaledObject` named `worker` | Reads the API inside the cluster |

The manifests are in [`infra/kubernetes/`](../infra/kubernetes/). `base/` holds the app, and `autoscaling/` adds the scaler. `inference/` adds model servers and Prometheus (see [`inference.md`](inference.md)), and `ci/` is the small setup that the CI workflow uses.

## How scaling works

Every 5 seconds, KEDA reads `backlog` from `/api/metrics`. The backlog is the number of jobs that need a worker now:

```text
backlog = ready jobs + running jobs + delayed jobs whose time has come
```

KEDA wants one worker for each job in the backlog, from 0 up to 5 workers.

- **Why not the Redis Lists scaler.** That scaler counts the items in one Redis key. Its documentation mentions only lists, but in KEDA 2.21 its script checks the key type and also counts sorted sets with `ZCARD`. One key is still not enough: the backlog adds the ready set, the running jobs, and only the part of the delayed set whose time has come. So KEDA reads the number the API already computes, with its Metrics API scaler.
- **Why running jobs count.** Workers also take back jobs whose lease ran out. If only ready jobs counted, the last worker could be removed while a dead worker's job still waits to be taken back.
- **Why due delayed jobs count.** Workers move delayed jobs into the ready queue when their time comes. With no workers, nobody would move them. Delayed jobs that are not due yet do not count, so a job delayed by an hour does not keep a worker running for an hour.

| Setting | Value | Why |
|---|---|---|
| `minReplicaCount` | 0 | No worker runs when there is no work |
| `maxReplicaCount` | 5 | The largest count in the earlier benchmark |
| `targetValue` | 1 | Each worker runs one job at a time |
| `pollingInterval` | 5 s | Starts the first worker quickly. Reading the metric is cheap |
| `cooldownPeriod` | 30 s | Wait after the last job before going back to zero |
| Scale-down window | 30 s | Kubernetes waits 5 minutes by default. 30 s is enough here, because workers start in a few seconds |
| `fallback` | 1 worker | If KEDA cannot read the metric 3 times, it keeps 1 worker so jobs still move |

## Removing workers safely

When KEDA removes a worker, Kubernetes sends it `SIGTERM`, waits `terminationGracePeriodSeconds`, then kills it.

1. The worker stops taking new jobs.
2. It waits up to `SHUTDOWN_GRACE_SECONDS` (35 s) for its job to finish. The longest job allowed is `sleep` with 30 s, so this is enough.
3. If the job is still running, the worker puts it back in the queue. That run does not count as a try.
4. Kubernetes waits 45 s in total before it kills the pod, so the steps above have time to finish.

If a pod is killed at once, its lease runs out after 30 s, and another worker takes the job.

## Checks

[`scripts/chaos_test.py`](../scripts/chaos_test.py) stops and kills workers while they run jobs, and counts the jobs before and after:

```bash
python -m scripts.chaos_test worker-scale-down worker-kill
```

| Test | What happens | Result on 2026-11-15 |
|---|---|---|
| `worker-scale-down` | 5 workers each run a 20 s job, then the count drops to 1 | 5 of 5 jobs succeeded on their first try. Each stopped worker finished its job before it exited |
| `worker-kill` | The pod running a 20 s job is deleted with no grace period | The job was taken back when its lease ran out and succeeded on its second try, 51 s after the kill |

Raw output: [`benchmarks/chaos/`](../benchmarks/chaos/).

## Results

[`scripts/burst_benchmark.py`](../scripts/burst_benchmark.py) sends one burst of jobs and compares KEDA with a fixed number of workers. For the fixed counts, it pauses KEDA at that count.

```bash
python -m scripts.burst_benchmark --trials 3
```

Measured on 2026-11-15 on one laptop (Intel i7-1260P, 16 CPUs and 7.6 GiB given to Docker). The workload was 10 s with no jobs, then 120 `sleep` jobs of 1 s sent at once, measured for 200 s. Each number is the median of 3 trials. Another kind cluster, from a different project, was running on the same Docker engine.

| Workers | Jobs done | Finish time | End-to-end p95 | First job start | Worker-seconds in 200 s |
|---|---:|---:|---:|---:|---:|
| KEDA, 0 to 5 | 360/360 | 32.9 s | 29.6 s | 7.2 s | 239 |
| Fixed 1 | 360/360 | 120.5 s | 113.7 s | 0 s | 200 |
| Fixed 3 | 360/360 | 40.2 s | 37.8 s | 0 s | 600 |
| Fixed 5 | 360/360 | 24.1 s | 21.8 s | 0 s | 1,000 |

- KEDA finished 8.8 s after 5 fixed workers, but used 76% less worker time.
- KEDA finished 7 s before 3 fixed workers, and used 60% less worker time.
- With no workers running, the first job started about 7 s after the burst. KEDA needs up to 5 s to see the jobs, and then the pod must start.
- Workers were back to zero about 61 s after the burst began, which is 28 s after the last job finished.
- No job was lost, failed, or run twice.

Worker-seconds stand in for cost: fixed workers are paid for even when the queue is empty. With longer quiet times between bursts, the gap would grow. Raw data: [`benchmarks/autoscaling/20261115T070731Z/`](../benchmarks/autoscaling/20261115T070731Z/).

## Security

- Redis and the API have cluster-internal addresses only. `kubectl port-forward` listens on `127.0.0.1`.
- A network policy lets only the API and worker pods reach Redis. A test pod in another namespace could not connect.
- The namespace enforces the `restricted` Pod Security level. Every container runs as a normal user, with a read-only file system, no extra Linux capabilities, and no Kubernetes access token.

## Limits

- One node on one laptop. All pods share the same CPU cores.
- When no worker runs, a new job waits for KEDA to notice it and for a pod to start.
- Redis is one pod. Its volume lives on the node's disk and is lost with the cluster.
