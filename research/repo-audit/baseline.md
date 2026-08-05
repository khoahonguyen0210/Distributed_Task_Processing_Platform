# Baseline

Recorded on 2026-10-10, before any project code was written.

## Repository state

| Item | Value |
|---|---|
| Commit | `0582f79` ("Initial commit") |
| Tracked files | `.gitattributes`, `.gitignore`, `LICENSE` (MIT), `README.md` (title only) |
| Untracked | Private planning notes (kept out of Git) |
| Source code | None |
| Tests | None |
| Docker files | None |

## Tools on the development machine

| Tool | Version | Notes |
|---|---|---|
| OS | Windows 11 Pro 10.0.26200 | |
| Python (default) | 3.14.7 | Not used for this project |
| Python (project) | 3.12.10 | Used for `.venv`, Docker image, and CI |
| Git | 2.55.0.windows.5 | |
| Docker | 29.7.2, Compose v5.5.1 | Docker Desktop had to be started first |
| Redis | 7.4.11 | Not installed on the host; runs in Docker (`redis:7.4-alpine`) |

## Blockers

None. Docker Desktop was not running at first, so `docker info` failed until it was started.
