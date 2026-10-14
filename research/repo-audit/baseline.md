# Starting Point

This is how the project looked on 2026-10-10, before any code was written.

## The repository

| Item | State |
|---|---|
| Commit | "Initial commit" |
| Files | `.gitattributes`, `.gitignore`, `LICENSE` (MIT), and a `README.md` with only a title |
| Kept out of Git | Private planning notes |
| Source code, tests, Docker files | None |

## Tools on the development machine

| Tool | Version | Notes |
|---|---|---|
| OS | Windows 11 Pro 10.0.26200 | |
| Python (default) | 3.14.7 | Not used for this project |
| Python (project) | 3.12.10 | Used for `.venv`, the Docker image, and CI |
| Git | 2.55.0.windows.5 | |
| Docker | 29.7.2, Compose v5.5.1 | Docker Desktop had to be started first |
| Redis | 7.4.11 | Not installed on the computer. It runs in Docker (`redis:7.4-alpine`) |

## Problems

None. Docker Desktop was not running at first, so `docker info` failed until it was started.
