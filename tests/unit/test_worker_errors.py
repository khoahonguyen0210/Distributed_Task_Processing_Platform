from pydantic import ValidationError

from app.jobs import UnknownJobTypeError, validate_payload
from app.workers.worker import describe_error


def test_error_description_hides_payload_values() -> None:
    try:
        validate_payload("add", {"a": "secret-value", "b": 1})
    except ValidationError as exc:
        text = describe_error(exc)
    assert text.startswith("Invalid payload: a:")
    assert "secret-value" not in text
    assert describe_error(RuntimeError("boom")) == "RuntimeError: boom"
    assert describe_error(UnknownJobTypeError("x")) == "Unknown job type: x"
