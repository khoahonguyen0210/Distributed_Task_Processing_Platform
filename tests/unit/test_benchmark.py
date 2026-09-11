from scripts.benchmark import job_rows, markdown_table, median_by_config, summarize


def jobs() -> list[dict[str, object]]:
    return [
        {"job_id": "a", "state": "succeeded", "attempts": 1, "created_at": 1000,
         "started_at": 1010, "finished_at": 1110},
        {"job_id": "b", "state": "succeeded", "attempts": 1, "created_at": 1000,
         "started_at": 1110, "finished_at": 2000},
        {"job_id": "c", "state": "dead_letter", "attempts": 3, "created_at": 1000,
         "started_at": 1200, "finished_at": 1300},
        {"job_id": "d", "state": "queued", "attempts": 0, "created_at": 1000,
         "started_at": None, "finished_at": None},
    ]  # fmt: skip


def test_job_rows_add_latency_columns() -> None:
    rows = job_rows(jobs())  # type: ignore[arg-type]
    assert (rows[0]["queue_wait_ms"], rows[0]["run_ms"], rows[0]["end_to_end_ms"]) == (10, 100, 110)
    assert rows[3]["queue_wait_ms"] is None and rows[3]["end_to_end_ms"] is None


def test_summary_counts_only_successful_jobs_for_latency() -> None:
    meta = {"workload": "sleep", "model": "process", "workers": 3, "trial": 1}
    s = summarize(job_rows(jobs()), {"total_retried": 2}, meta)  # type: ignore[arg-type]
    assert (s.submitted, s.succeeded, s.dead_letter, s.retried) == (4, 2, 1, 2)
    assert s.elapsed_s == 1.0
    assert s.throughput_per_s == 2.0
    assert s.end_to_end_p50_ms == 110 and s.end_to_end_p95_ms == 1000


def test_medians_and_table_have_one_row_per_config() -> None:
    meta = {"workload": "cpu", "model": "thread", "workers": 1}
    trials = [summarize(job_rows(jobs()), {}, {**meta, "trial": t}) for t in (1, 2, 3)]  # type: ignore[arg-type]
    medians = median_by_config(trials)
    assert len(medians) == 1
    assert medians[0]["trials"] == 3 and medians[0]["throughput_per_s"] == 2.0
    table = markdown_table(medians)
    assert table.count("\n") == 3 and "| cpu | thread | 1 | 6/12 |" in table
