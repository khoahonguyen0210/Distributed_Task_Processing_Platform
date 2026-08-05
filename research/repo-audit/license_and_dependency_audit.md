# License and Dependency Audit

## Reference repositories

The license files were read directly from each clone. Two of the early planning notes were wrong, and the table below shows the verified licenses.

| Repository | License file | Verified license | Planning note said | Copied code |
|---|---|---|---|---|
| Celery | `LICENSE` | BSD-3-Clause | BSD-3-Clause | None |
| RQ | `LICENSE`, `pyproject.toml` | BSD-2-Clause | BSD-3-Clause (wrong) | None |
| Dramatiq | `COPYING`, `COPYING.LESSER`, `setup.py` | LGPL-3.0-or-later | Apache-2.0 (wrong) | None |

Because no code is copied, none of these licenses adds duties to this project.

## Project dependencies

This project is MIT licensed. Its runtime dependencies are declared in `pyproject.toml` with lower and upper bounds.

| Package | Version tested | License | Role |
|---|---|---|---|
| fastapi | 0.143.0 | MIT | HTTP API and WebSocket routes |
| starlette | 1.7.0 | BSD-3-Clause | Used by FastAPI |
| pydantic | 2.14.0 | MIT | Request and payload validation |
| pydantic-settings | 2.15.0 | MIT | Settings from environment variables |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI server |
| websockets | 17.2 | BSD-3-Clause | WebSocket support for uvicorn |
| redis (redis-py) | 7.4.1 | MIT | Redis client |

Development only:

| Package | Version tested | License |
|---|---|---|
| pytest | 9.1.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| ruff | 0.17.0 | MIT |
| mypy | 1.20.2 | MIT |

Services:

| Service | Version | License | Note |
|---|---|---|---|
| Redis server | 7.4.11 (`redis:7.4-alpine`) | RSALv2 / SSPLv1 dual license since 7.4 | Run as a separate, unmodified container for local use. No Redis code is shipped in this repository |
| Python image | `python:3.12-slim` | PSF and Debian package licenses | Base image for the app container |

## Note on Redis 7.4

Redis 7.4 and later are not under the BSD license. Running an unmodified Redis server locally for development is allowed under those terms. If a fully open source server is needed, Valkey (BSD-3-Clause) speaks the same protocol and should work with the same commands, but it was not tested here.
