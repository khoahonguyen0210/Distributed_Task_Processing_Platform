# Licenses

## Projects that were read

Each license file was opened and read in the clone. Two early planning notes named the wrong license, so this table shows what the files really say.

| Project | License file | License | Early note said | Code copied |
|---|---|---|---|---|
| Celery | `LICENSE` | BSD-3-Clause | BSD-3-Clause | None |
| RQ | `LICENSE`, `pyproject.toml` | BSD-2-Clause | BSD-3-Clause (wrong) | None |
| Dramatiq | `COPYING`, `COPYING.LESSER`, `setup.py` | LGPL-3.0-or-later | Apache-2.0 (wrong) | None |

No code is copied, so none of these licenses puts any duty on this project.

## Packages this project uses

This project is MIT licensed. `pyproject.toml` lists each package with a lowest and a highest allowed version.

| Package | Version tested | License | Used for |
|---|---|---|---|
| fastapi | 0.143.0 | MIT | HTTP API and WebSocket routes |
| starlette | 1.7.0 | BSD-3-Clause | Used by FastAPI |
| pydantic | 2.14.0 | MIT | Checking requests and job payloads |
| pydantic-settings | 2.15.0 | MIT | Reading settings from environment variables |
| uvicorn | 0.54.0 | BSD-3-Clause | Web server |
| websockets | 17.2 | BSD-3-Clause | WebSocket support for uvicorn |
| redis (redis-py) | 7.4.1 | MIT | Redis client |

Only for development:

| Package | Version tested | License |
|---|---|---|
| pytest | 9.1.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| ruff | 0.17.0 | MIT |
| mypy | 1.20.2 | MIT |

Services:

| Service | Version | License | Note |
|---|---|---|---|
| Redis server | 7.4.11 (`redis:7.4-alpine`) | RSALv2 / SSPLv1 dual license since 7.4 | Runs as a separate, unmodified container for local use. No Redis code is in this repository |
| Python image | `python:3.12-slim` | PSF and Debian package licenses | Base image of the app container |

## A note on Redis 7.4

Redis 7.4 and later are not under the BSD license. Running an unmodified Redis server on your own computer for development is allowed under the new terms. If you need a fully open source server, Valkey (BSD-3-Clause) speaks the same protocol and should work with the same commands, but it was not tested here.
