import os
import uuid
from collections.abc import Iterator

import pytest
import redis

from app.core.config import Settings
from app.queue.redis_queue import RedisQueue

# Tests use their own database and a random key prefix, so they never touch real data.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.redis)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "redis_url": TEST_REDIS_URL,
        "key_prefix": f"test:{uuid.uuid4().hex[:12]}:",
        "lease_seconds": 2.0,
        "heartbeat_seconds": 0.2,
        "worker_stale_seconds": 1.0,
        "idle_wait_seconds": 0.2,
        "maintenance_seconds": 0.1,
        "retry_base_seconds": 0.1,
        "retry_max_seconds": 0.5,
        "shutdown_grace_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip(f"Redis is not reachable at {TEST_REDIS_URL}")
    yield client
    client.close()


def clear_prefix(client: redis.Redis, prefix: str) -> None:
    keys = list(client.scan_iter(match=f"{prefix}*", count=500))
    if keys:
        client.delete(*keys)


@pytest.fixture
def settings(redis_client: redis.Redis) -> Iterator[Settings]:
    s = make_settings()
    yield s
    clear_prefix(redis_client, s.key_prefix)


@pytest.fixture
def queue(redis_client: redis.Redis, settings: Settings) -> RedisQueue:
    return RedisQueue(redis_client, settings)
