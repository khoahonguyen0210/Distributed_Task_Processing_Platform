from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def test_api_returns_503_when_redis_is_down() -> None:
    settings = make_settings(redis_url="redis://127.0.0.1:1/0")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/jobs", json={"type": "add", "payload": {"a": 1, "b": 2}})
        assert response.status_code == 503
        assert response.json() == {"detail": "Redis is not available. Try again later."}
        assert client.get("/api/jobs").status_code == 503


def test_validation_still_works_when_redis_is_down() -> None:
    settings = make_settings(redis_url="redis://127.0.0.1:1/0")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/jobs", json={"type": "nope", "payload": {}})
        assert response.status_code == 422
