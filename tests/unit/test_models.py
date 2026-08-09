import random

import pytest

from app.queue.backoff import retry_delay
from app.queue.models import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_STATES,
    TERMINAL_STATES,
    Job,
    JobState,
    is_allowed,
)


def test_every_state_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == {None, *JobState}


def test_terminal_states_have_no_exit_except_dead_letter_replay() -> None:
    for state in TERMINAL_STATES:
        expected = {JobState.QUEUED} if state == JobState.DEAD_LETTER else set()
        assert ALLOWED_TRANSITIONS[state] == expected


def test_running_jobs_cannot_be_cancelled() -> None:
    assert JobState.RUNNING not in CANCELLABLE_STATES
    assert not is_allowed(JobState.RUNNING, JobState.CANCELLED)


def test_invalid_transitions_are_rejected() -> None:
    assert not is_allowed(JobState.SUCCEEDED, JobState.RUNNING)
    assert not is_allowed(JobState.QUEUED, JobState.SUCCEEDED)
    assert not is_allowed(None, JobState.RUNNING)
    assert is_allowed(JobState.QUEUED, JobState.RUNNING)


def test_job_from_hash_parses_types() -> None:
    job = Job.from_hash(
        {
            "id": "abc",
            "type": "add",
            "payload": '{"a": 1, "b": 2}',
            "state": "succeeded",
            "priority": "7",
            "attempts": "1",
            "max_attempts": "3",
            "created_at": "1000",
            "updated_at": "2000",
            "started_at": "1500",
            "finished_at": "2000",
            "result": "3",
        }
    )
    assert job.payload == {"a": 1, "b": 2}
    assert job.state is JobState.SUCCEEDED
    assert (job.priority, job.result, job.started_at, job.eligible_at) == (7, 3, 1500, None)


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 8, 20])
def test_retry_delay_stays_inside_bounds(attempt: int) -> None:
    rng = random.Random(attempt)
    ceiling = min(30.0, 1.0 * 2 ** (attempt - 1))
    for _ in range(200):
        delay = retry_delay(attempt, base=1.0, cap=30.0, rng=rng)
        assert ceiling / 2 <= delay <= ceiling


def test_retry_delay_grows_then_caps() -> None:
    assert retry_delay(1, 1.0, 30.0, random.Random(0)) <= 1.0
    assert retry_delay(10, 1.0, 30.0, random.Random(0)) <= 30.0


def test_retry_delay_rejects_attempt_zero() -> None:
    with pytest.raises(ValueError):
        retry_delay(0, 1.0, 30.0)
