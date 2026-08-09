import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.DEAD_LETTER, JobState.CANCELLED}
)

# The only moves the Lua scripts make. Tests check every published event against this table.
ALLOWED_TRANSITIONS: dict[JobState | None, frozenset[JobState]] = {
    None: frozenset({JobState.QUEUED, JobState.SCHEDULED}),
    JobState.SCHEDULED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.RETRY_SCHEDULED,
            JobState.DEAD_LETTER,
            JobState.QUEUED,
        }
    ),
    JobState.RETRY_SCHEDULED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.DEAD_LETTER: frozenset({JobState.QUEUED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}

CANCELLABLE_STATES = frozenset({JobState.SCHEDULED, JobState.QUEUED, JobState.RETRY_SCHEDULED})


def is_allowed(previous: JobState | None, new: JobState) -> bool:
    return new in ALLOWED_TRANSITIONS[previous]


def _int(value: str | None) -> int | None:
    return int(value) if value else None


@dataclass(slots=True)
class Job:
    id: str
    type: str
    payload: dict[str, Any]
    state: JobState
    priority: int
    attempts: int
    max_attempts: int
    created_at: int
    updated_at: int
    eligible_at: int | None = None
    started_at: int | None = None
    finished_at: int | None = None
    worker_id: str | None = None
    lease_token: str | None = None
    result: Any = None
    error: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_hash(cls, data: dict[str, str]) -> "Job":
        result = data.get("result")
        return cls(
            id=data["id"],
            type=data["type"],
            payload=json.loads(data.get("payload") or "{}"),
            state=JobState(data["state"]),
            priority=int(data["priority"]),
            attempts=int(data["attempts"]),
            max_attempts=int(data["max_attempts"]),
            created_at=int(data["created_at"]),
            updated_at=int(data["updated_at"]),
            eligible_at=_int(data.get("eligible_at")),
            started_at=_int(data.get("started_at")),
            finished_at=_int(data.get("finished_at")),
            worker_id=data.get("worker_id") or None,
            lease_token=data.get("lease_token") or None,
            result=json.loads(result) if result else None,
            error=data.get("error") or None,
        )
