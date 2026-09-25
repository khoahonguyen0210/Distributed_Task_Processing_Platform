from fastapi import status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

BODY_METHODS = {"POST", "PUT", "PATCH"}


class BodyLimit:
    """Refuses large request bodies before they are read into memory."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in BODY_METHODS:
            headers = dict(scope["headers"])
            length = headers.get(b"content-length")
            if length is None and b"transfer-encoding" in headers:
                response = JSONResponse(
                    {"detail": "Send a Content-Length header."},
                    status.HTTP_411_LENGTH_REQUIRED,
                )
                await response(scope, receive, send)
                return
            if length is not None and (not length.isdigit() or int(length) > self.limit):
                response = JSONResponse(
                    {"detail": f"The request body is over the {self.limit}-byte limit."},
                    status.HTTP_413_CONTENT_TOO_LARGE,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
