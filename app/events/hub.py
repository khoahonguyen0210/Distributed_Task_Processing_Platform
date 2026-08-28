import asyncio
import contextlib
import json
import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

log = logging.getLogger("taskq.events")

RESYNC = json.dumps({"type": "resync"})


class Subscriber:
    """One browser connection. If it falls behind, old events are dropped and it must resync."""

    def __init__(self, size: int) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=size)

    def offer(self, message: str) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(RESYNC)

    async def get(self) -> str:
        return await self.queue.get()


class EventHub:
    """Reads job events from Redis Pub/Sub once and copies them to every subscriber.

    Pub/Sub keeps nothing: events sent while the hub is disconnected are lost.
    After a reconnect every subscriber gets a resync message and reloads state from the API.
    """

    def __init__(self, redis_url: str, channel: str, buffer_size: int = 256) -> None:
        self.redis_url = redis_url
        self.channel = channel
        self.buffer_size = buffer_size
        self.subscribers: set[Subscriber] = set()
        self.connected = False
        self._task: asyncio.Task[None] | None = None

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self.buffer_size)
        self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def publish_local(self, message: str) -> None:
        for sub in list(self.subscribers):
            sub.offer(message)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        pause = 0.5
        while True:
            client = aioredis.Redis.from_url(self.redis_url, decode_responses=True)
            try:
                async with client.pubsub() as pubsub:
                    await pubsub.subscribe(self.channel)
                    if not self.connected:
                        self.connected = True
                        pause = 0.5
                        self.publish_local(RESYNC)
                        log.info("event hub connected", extra={"event": "hub_connected"})
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            self.publish_local(
                                json.dumps({**json.loads(message["data"]), "type": "job"})
                            )
            except (RedisError, OSError) as exc:
                if self.connected:
                    log.warning(
                        "event hub lost Redis: %s", exc, extra={"event": "hub_disconnected"}
                    )
                self.connected = False
                await asyncio.sleep(pause)
                pause = min(pause * 2, 10.0)
            finally:
                await client.aclose()
