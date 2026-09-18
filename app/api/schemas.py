from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.queue.models import Job, JobState
from app.queue.redis_queue import MAX_DELAY_SECONDS


class SubmitJob(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"type": "add", "payload": {"a": 2, "b": 3}}]},
    )

    type: str = Field(min_length=1, max_length=64, description="A registered job type.")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=0, le=9, description="9 runs first.")
    delay_seconds: float = Field(
        default=0, ge=0, le=MAX_DELAY_SECONDS, description="Wait before running."
    )
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class JobOut(BaseModel):
    """Times are Unix time in milliseconds, taken from the Redis server clock."""

    id: str
    type: str
    state: JobState
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    created_at: int
    updated_at: int
    eligible_at: int | None
    started_at: int | None
    finished_at: int | None
    worker_id: str | None
    result: Any
    error: str | None

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(
            id=job.id,
            type=job.type,
            state=job.state,
            payload=job.payload,
            priority=job.priority,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            created_at=job.created_at,
            updated_at=job.updated_at,
            eligible_at=job.eligible_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            worker_id=job.worker_id,
            result=job.result,
            error=job.error,
        )


class HistoryEntry(BaseModel):
    ts: int
    event: str
    state: str
    attempt: int
    worker_id: str
    detail: str


class JobDetail(JobOut):
    history: list[HistoryEntry]

    @classmethod
    def of(cls, job: Job) -> "JobDetail":
        base = JobOut.of(job).model_dump()
        return cls(**base, history=[HistoryEntry(**h) for h in job.history])


class JobList(BaseModel):
    jobs: list[JobOut]
    limit: int
    offset: int


class JobType(BaseModel):
    name: str
    description: str
    payload_schema: dict[str, Any]
