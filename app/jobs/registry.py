from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class PermanentJobError(Exception):
    """Raise from a handler when trying again cannot help. The job fails without a retry."""


class UnknownJobTypeError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class JobContext:
    job_id: str
    attempt: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    description: str
    payload_model: type[BaseModel]
    run: Callable[[Any, JobContext], Any]


REGISTRY: dict[str, JobSpec] = {}


def register[M: BaseModel](
    name: str, description: str, model: type[M]
) -> Callable[[Callable[[M, JobContext], Any]], Callable[[M, JobContext], Any]]:
    """Adds a handler to the registry. Workers run only handlers added this way."""

    def wrap(func: Callable[[M, JobContext], Any]) -> Callable[[M, JobContext], Any]:
        if name in REGISTRY:
            raise ValueError(f"job type {name!r} is already registered")
        REGISTRY[name] = JobSpec(name, description, model, func)
        return func

    return wrap


def get_spec(job_type: str) -> JobSpec:
    try:
        return REGISTRY[job_type]
    except KeyError:
        raise UnknownJobTypeError(job_type) from None


def validate_payload(job_type: str, payload: dict[str, Any]) -> BaseModel:
    """Raises UnknownJobTypeError or pydantic.ValidationError."""
    return get_spec(job_type).payload_model.model_validate(payload)
