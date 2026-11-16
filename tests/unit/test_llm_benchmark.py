from scripts.cluster import Sample
from scripts.llm_benchmark import job_rows, markdown_table, medians, parse_mode, summarize


def test_modes_name_servers_and_admission() -> None:
    assert parse_mode("fixed-2") == (2, True)
    assert parse_mode("auto") == (None, True)
    assert parse_mode("fixed-3-open") == (3, False)
    assert parse_mode("auto-open") == (None, False)


def rows() -> list[dict[str, object]]:
    jobs: list[dict[str, object] | None] = [
        {
            "state": "succeeded",
            "attempts": 1,
            "created_at": 0,
            "started_at": 1000,
            "finished_at": 3000,
            "result": {"completion_tokens": 40},
        },
        {
            "state": "succeeded",
            "attempts": 2,
            "created_at": 0,
            "started_at": 2000,
            "finished_at": 5000,
            "result": {"completion_tokens": 60},
        },
        {
            "state": "expired",
            "attempts": 0,
            "created_at": 0,
            "started_at": None,
            "finished_at": 9000,
            "result": None,
        },
        {
            "state": "running",
            "attempts": 1,
            "created_at": 0,
            "started_at": 4000,
            "finished_at": None,
            "result": None,
        },
    ]
    return job_rows(jobs, ["a", "b", "c", "d"])


def test_summary_counts_outcomes_tokens_and_scaling() -> None:
    samples = [Sample(0, 1, 1, 5, 3), Sample(5, 2, 1, 5, 3), Sample(8, 2, 2, 5, 1)]
    s = summarize("auto", 1, 6, rows(), samples, 10)
    assert (s["offered"], s["accepted"], s["rejected"]) == (6, 4, 2)
    assert (s["succeeded"], s["expired"], s["lost"]) == (2, 1, 1)
    assert s["retried_runs"] == 1
    assert s["end_to_end_p50_s"] == 3.0 and s["end_to_end_p95_s"] == 5.0
    assert s["output_tokens_per_s"] == 25.0
    assert s["first_scale_up_s"] == 8 and s["peak_servers"] == 2
    assert s["scale_down_s"] is None
    assert s["server_seconds"] == 1 * 5 + 2 * 3 + 2 * 2


def test_summary_finds_scale_down() -> None:
    samples = [Sample(0, 1, 1, 5, 0), Sample(5, 2, 2, 5, 4), Sample(30, 1, 2, 5, 0)]
    s = summarize("auto", 1, 6, rows(), samples, 40)
    assert s["first_scale_up_s"] == 5 and s["scale_down_s"] == 30


def test_summary_without_successes() -> None:
    s = summarize("fixed-1", 1, 1, [], [], 10)
    assert s["succeeded"] == 0 and s["output_tokens_per_s"] is None


def test_medians_and_table() -> None:
    trials = [summarize("fixed-1", t, 6, rows(), [], 10) for t in (1, 2)]
    table = medians(trials)
    assert table[0]["offered"] == 12 and table[0]["rejected"] == 4 and table[0]["lost"] == 2
    assert "| fixed-1 | 12 | 4 | 2 | 4 | 2 |" in markdown_table(table)
