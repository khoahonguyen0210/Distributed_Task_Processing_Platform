# Observability

This page lists what Prometheus collects in the Kubernetes setup, the queries used for scaling and for the benchmark, and how the latency goal was chosen.

## What Prometheus reads

Prometheus runs inside the cluster and reads three kinds of targets every 5 seconds. None of them can be reached from outside the cluster.

| Target | Address | Main metrics |
|---|---|---|
| API | `api.taskq.svc.cluster.local:8000/metrics` | Queue sizes, job counts, end-to-end time of successful jobs |
| Workers | Each worker pod, port 9100 | Slots, running jobs, finished and failed jobs |
| Model servers | Each model-server pod, port 8080 | Requests running, requests waiting for a slot, tokens made |

The API reads the counts from Redis, so they cover all workers together. The end-to-end histogram is kept in Redis too: each successful job adds its time from submission to success, in one step with the change to `succeeded`.

## Queries

| What | PromQL |
|---|---|
| Jobs waiting to run | `taskq_queue_jobs{queue="ready"}` |
| Jobs that need a worker now | `taskq_backlog_jobs` |
| Requests waiting for a model-server slot | `sum(llamacpp:requests_deferred)` |
| Requests the model servers are running | `sum(llamacpp:requests_processing)` |
| LLM jobs finished per second | `sum(rate(taskq_job_end_to_end_seconds_count{type="llm_generate"}[1m]))` |
| p95 end-to-end time of LLM jobs | `histogram_quantile(0.95, sum by (le) (rate(taskq_job_end_to_end_seconds_bucket{type="llm_generate"}[1m])))` |
| Tokens made per second | `sum(rate(llamacpp:tokens_predicted_total[1m]))` |
| Jobs refused because the queue was full | `rate(taskq_jobs_total{event="rejected"}[1m])` |
| Jobs that expired before they started | `rate(taskq_jobs_total{event="expired"}[1m])` |
| Jobs running in workers | `sum(taskq_worker_active_jobs)` |

To try a query, run `kubectl -n taskq port-forward svc/prometheus 9090:9090` and open <http://127.0.0.1:9090>.

The KEDA queries add `or vector(0)`, so they still return a number when there is no data. The p95 query also keeps only values above 0, because `histogram_quantile` returns "not a number" when no job finished in the last minute.

## The latency goal

The goal (an SLO, or service level objective) is:

> **95% of `llm_generate` jobs finish within 25 s of submission.**

It comes from a measured baseline, not a guess:

| Baseline run | Value |
|---|---|
| Setup | 1 model server, 5 workers, no queue limit |
| Load | 1 job every 10 s for 300 s (30 jobs, 64 tokens each), about a third of what one server can do (0.287 jobs/s) |
| Result | 30 of 30 succeeded. p50 7.0 s, p95 12.3 s, p99 16.0 s |

The goal is twice the baseline p95, rounded up to the next 5 s. During a burst, a job may also wait about one job time for a free slot, and the doubled value leaves room for that and for the uneven speed of this laptop.

An earlier, shorter baseline (180 s, 18 jobs) gave p95 31.1 s. With 18 jobs, p95 is simply the slowest job, so the longer run replaced it. Both runs are in the build notes.

The goal is used in three places: the p95 trigger of the model-server scaler, the queue limit in [`inference.md`](inference.md), and the benchmark report. Raw data: [`benchmarks/inference/baseline/`](../benchmarks/inference/baseline/).
