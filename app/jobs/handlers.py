"""Built-in job types. They are safe, bounded, and give the same result when run twice."""

import time

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.registry import JobContext, PermanentJobError, register


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AddPayload(Payload):
    a: float = Field(ge=-1e12, le=1e12)
    b: float = Field(ge=-1e12, le=1e12)


class SleepPayload(Payload):
    seconds: float = Field(ge=0, le=30)


class CpuPayload(Payload):
    n: int = Field(ge=1, le=5_000_000)


class FlakyPayload(Payload):
    fail_times: int = Field(ge=0, le=10)


class FailPayload(Payload):
    message: str = Field(min_length=1, max_length=200)


@register("add", "Adds two numbers.", AddPayload)
def add(p: AddPayload, ctx: JobContext) -> float:
    return p.a + p.b


@register("sleep", "Waits for a number of seconds, like a slow network call.", SleepPayload)
def sleep(p: SleepPayload, ctx: JobContext) -> dict[str, float]:
    time.sleep(p.seconds)
    return {"slept": p.seconds}


@register("cpu", "Runs a fixed loop of n steps to keep one CPU core busy.", CpuPayload)
def cpu(p: CpuPayload, ctx: JobContext) -> dict[str, int]:
    checksum = 0
    for i in range(p.n):
        checksum = (checksum + i * i) % 1_000_003
    return {"n": p.n, "checksum": checksum}


@register("flaky", "Fails on its first fail_times attempts, then succeeds.", FlakyPayload)
def flaky(p: FlakyPayload, ctx: JobContext) -> dict[str, int]:
    if ctx.attempt <= p.fail_times:
        raise RuntimeError(f"planned failure on attempt {ctx.attempt} of {p.fail_times}")
    return {"succeeded_on_attempt": ctx.attempt}


@register("fail", "Always fails with an error that is not retried.", FailPayload)
def fail(p: FailPayload, ctx: JobContext) -> None:
    raise PermanentJobError(p.message)
