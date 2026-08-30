from importlib import resources

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse

STATIC = resources.files("app.dashboard") / "static"

# The page loads only its own files and talks only to this server.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}

FILES = {"app.js": "text/javascript", "style.css": "text/css"}

router = APIRouter(include_in_schema=False)


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/dashboard/")


@router.get("/dashboard/")
def dashboard() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"), headers=SECURITY_HEADERS)


@router.get("/dashboard/{name}")
def asset(name: str) -> FileResponse:
    if name not in FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return FileResponse(str(STATIC / name), media_type=FILES[name], headers=SECURITY_HEADERS)
