from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def client() -> TestClient:
    return TestClient(create_app(make_settings(redis_url="redis://127.0.0.1:1/0")))


def test_root_redirects_to_dashboard() -> None:
    with client() as c:
        response = c.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/dashboard/"


def test_dashboard_page_and_assets_are_served_with_strict_headers() -> None:
    with client() as c:
        page = c.get("/dashboard/")
        assert page.status_code == 200
        assert "<title>taskq dashboard</title>" in page.text
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert c.get("/dashboard/app.js").headers["content-type"].startswith("text/javascript")
        assert c.get("/dashboard/style.css").headers["content-type"].startswith("text/css")


def test_dashboard_serves_only_known_files() -> None:
    with client() as c:
        assert c.get("/dashboard/routes.py").status_code == 404
        assert c.get("/dashboard/..%2Froutes.py").status_code == 404
