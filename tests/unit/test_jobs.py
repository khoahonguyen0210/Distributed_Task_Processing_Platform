import pytest
from pydantic import ValidationError

from app.jobs import REGISTRY, JobContext, PermanentJobError, UnknownJobTypeError, validate_payload

CTX = JobContext(job_id="j", attempt=1, max_attempts=3)


def run(job_type: str, payload: dict[str, object], ctx: JobContext = CTX) -> object:
    spec = REGISTRY[job_type]
    return spec.run(validate_payload(job_type, payload), ctx)


def test_builtin_types_are_registered() -> None:
    assert set(REGISTRY) == {"add", "sleep", "cpu", "flaky", "fail"}


def test_add_and_cpu_are_deterministic() -> None:
    assert run("add", {"a": 2, "b": 3.5}) == 5.5
    assert run("cpu", {"n": 1000}) == run("cpu", {"n": 1000})


def test_sleep_returns_seconds() -> None:
    assert run("sleep", {"seconds": 0}) == {"slept": 0}


def test_flaky_fails_until_its_attempt_comes() -> None:
    with pytest.raises(RuntimeError):
        run("flaky", {"fail_times": 2}, JobContext("j", 2, 3))
    assert run("flaky", {"fail_times": 2}, JobContext("j", 3, 3)) == {"succeeded_on_attempt": 3}


def test_fail_is_permanent() -> None:
    with pytest.raises(PermanentJobError):
        run("fail", {"message": "no"})


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(UnknownJobTypeError):
        validate_payload("os.system", {})


@pytest.mark.parametrize(
    ("job_type", "payload"),
    [
        ("add", {"a": 1}),
        ("add", {"a": 1, "b": 2, "c": 3}),
        ("add", {"a": "1", "b": 2}),
        ("add", {"a": 1e13, "b": 0}),
        ("sleep", {"seconds": 31}),
        ("sleep", {"seconds": True}),
        ("cpu", {"n": 0}),
        ("cpu", {"n": 10_000_000}),
        ("flaky", {"fail_times": -1}),
        ("fail", {"message": ""}),
        ("fail", {"message": "x" * 201}),
    ],
)
def test_bad_payloads_are_rejected(job_type: str, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        validate_payload(job_type, payload)
