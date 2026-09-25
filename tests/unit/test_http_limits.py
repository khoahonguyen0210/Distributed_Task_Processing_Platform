from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def client(**overrides: object) -> TestClient:
    return TestClient(create_app(make_settings(redis_url="redis://127.0.0.1:1/0", **overrides)))


def test_unknown_host_is_refused() -> None:
    with client() as c:
        assert c.get("/health/live").status_code == 200
        assert c.get("/health/live", headers={"host": "evil.example"}).status_code == 400


def test_large_body_is_refused_before_it_is_read() -> None:
    with client(max_body_bytes=1024) as c:
        body = '{"type": "add", "payload": {"pad": "' + "x" * 2000 + '"}}'
        response = c.post("/api/jobs", content=body, headers={"content-type": "application/json"})
        assert response.status_code == 413


def test_chunked_body_without_length_is_refused() -> None:
    with client() as c:
        response = c.post(
            "/api/jobs",
            content=iter([b'{"type": "add", ', b'"payload": {}}']),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 411


def test_text_plain_post_from_another_site_is_rejected() -> None:
    with client() as c:
        body = '{"type": "add", "payload": {"a": 1, "b": 2}}'
        response = c.post("/api/jobs", content=body, headers={"content-type": "text/plain"})
        assert response.status_code == 422
