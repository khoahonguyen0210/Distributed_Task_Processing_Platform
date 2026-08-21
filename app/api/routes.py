from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import ValidationError

from app.api.schemas import JobDetail, JobList, JobOut, JobType, SubmitJob
from app.jobs import REGISTRY, UnknownJobTypeError, validate_payload
from app.queue.models import JobState
from app.queue.redis_queue import PayloadTooLargeError, RedisQueue

router = APIRouter(prefix="/api", tags=["jobs"])

JobId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="32 hex characters")]


def get_queue(request: Request) -> RedisQueue:
    queue: RedisQueue = request.app.state.queue
    return queue


def clean_errors(exc: ValidationError) -> list[dict[str, object]]:
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


@router.get("/job-types", response_model=list[JobType])
def list_job_types() -> list[JobType]:
    return [
        JobType(
            name=spec.name,
            description=spec.description,
            payload_schema=spec.payload_model.model_json_schema(),
        )
        for spec in REGISTRY.values()
    ]


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def submit_job(body: SubmitJob, request: Request) -> JobOut:
    try:
        validate_payload(body.type, body.payload)
    except UnknownJobTypeError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown job type. Choose one of: {', '.join(sorted(REGISTRY))}",
        ) from None
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, clean_errors(exc)) from None
    try:
        job = get_queue(request).enqueue(
            body.type, body.payload, body.priority, body.delay_seconds, body.max_attempts
        )
    except PayloadTooLargeError as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"Payload is too large: {exc}"
        ) from None
    return JobOut.of(job)


@router.get("/jobs", response_model=JobList)
def list_jobs(
    request: Request,
    state: JobState | None = None,
    type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> JobList:
    jobs = get_queue(request).list_jobs(state, type, limit, offset)
    return JobList(jobs=[JobOut.of(j) for j in jobs], limit=limit, offset=offset)


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: JobId, request: Request) -> JobDetail:
    job = get_queue(request).get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return JobDetail.of(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: JobId, request: Request) -> JobOut:
    """Cancels a job that has not started. Running and finished jobs cannot be cancelled."""
    queue = get_queue(request)
    cancelled, state = queue.cancel(job_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if not cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {state} job cannot be cancelled")
    job = queue.get(job_id, with_history=False)
    assert job is not None
    return JobOut.of(job)


@router.get("/dead-letters", response_model=JobList)
def list_dead_letters(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> JobList:
    jobs = get_queue(request).dead_letters(limit, offset)
    return JobList(jobs=[JobOut.of(j) for j in jobs], limit=limit, offset=offset)


@router.post("/dead-letters/{job_id}/replay", response_model=JobOut)
def replay_dead_letter(job_id: JobId, request: Request) -> JobOut:
    """Queues a dead-letter job again with a fresh attempt count. Its history is kept."""
    queue = get_queue(request)
    if not queue.replay(job_id):
        job = queue.get(job_id, with_history=False)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {job.state} job cannot be replayed")
    job = queue.get(job_id, with_history=False)
    assert job is not None
    return JobOut.of(job)
