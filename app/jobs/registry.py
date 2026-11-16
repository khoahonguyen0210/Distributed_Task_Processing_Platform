from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings, get_settings


class PermanentJobError(Exception):
    pass


class UnknownJobTypeError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class JobContext:
    job_id: str
    attempt: int
    max_attempts: int
    settings: Settings | None = None

    @property
    def config(self) -> Settings:
        return self.settings or get_settings()


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    description: str
    payload_model: type[BaseModel]
    run: Callable[[Any, JobContext], Any]
    deadline_seconds: float | None = None


REGISTRY: dict[str, JobSpec] = {}


def register[M: BaseModel](
    name: str, description: str, model: type[M], deadline_seconds: float | None = None
) -> Callable[[Callable[[M, JobContext], Any]], Callable[[M, JobContext], Any]]:
    def wrap(func: Callable[[M, JobContext], Any]) -> Callable[[M, JobContext], Any]:
        if name in REGISTRY:
            raise ValueError(f"job type {name!r} is already registered")
        REGISTRY[name] = JobSpec(name, description, model, func, deadline_seconds)
        return func

    return wrap


def get_spec(job_type: str) -> JobSpec:
    try:
        return REGISTRY[job_type]
    except KeyError:
        raise UnknownJobTypeError(job_type) from None


def validate_payload(job_type: str, payload: dict[str, Any]) -> BaseModel:
    return get_spec(job_type).payload_model.model_validate(payload)
