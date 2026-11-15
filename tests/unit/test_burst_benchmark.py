from scripts.burst_benchmark import (
    Sample,
    job_rows,
    markdown_table,
    medians,
    replica_seconds,
    summarize,
)


def samples() -> list[Sample]:
    return [
        Sample(-5.0, 0, 0, 0, 0),
        Sample(0.0, 0, 0, 0, 4),
        Sample(2.0, 2, 1, 1, 4),
        Sample(4.0, 2, 2, 2, 2),
        Sample(10.0, 0, 0, 0, 0),
    ]


def jobs() -> list[dict[str, object] | None]:
    return [
        {
            "state": "succeeded",
            "attempts": 1,
            "created_at": 0,
            "started_at": 3000,
            "finished_at": 4000,
        },
        {
            "state": "succeeded",
            "attempts": 2,
            "created_at": 0,
            "started_at": 3000,
            "finished_at": 6000,
        },
        {
            "state": "running",
            "attempts": 1,
            "created_at": 0,
            "started_at": 5000,
            "finished_at": None,
        },
        None,
    ]


def test_replica_seconds_counts_each_sample_until_the_next() -> None:
    assert replica_seconds(samples(), 0, 10) == 2 * 2 + 2 * 6
    assert replica_seconds(samples(), 0, 12) == 16
    assert replica_seconds([], 0, 10) == 0


def test_summary_counts_lost_jobs_and_timings() -> None:
    rows = job_rows(jobs(), ["a", "b", "c", "d"])
    s = summarize("keda", 1, rows, samples(), 10)
    assert s["submitted"] == 4 and s["succeeded"] == 2 and s["lost"] == 2
    assert s["retried_runs"] == 1
    assert s["completion_s"] == 6.0
    assert s["first_job_start_s"] == 3.0
    assert s["first_ready_worker_s"] == 2.0
    assert s["peak_replicas"] == 2
    assert s["back_to_zero_s"] == 10.0
    assert s["end_to_end_p50_s"] == 4.0 and s["end_to_end_p95_s"] == 6.0


def test_medians_and_table() -> None:
    rows = job_rows(jobs(), ["a", "b", "c", "d"])
    trials = [summarize("fixed-1", t, rows, samples(), 10) for t in (1, 2, 3)]
    table = medians(trials)
    assert table[0]["trials"] == 3 and table[0]["submitted"] == 12 and table[0]["lost"] == 6
    assert "| fixed-1 | 6/12 | 6 |" in markdown_table(table)
