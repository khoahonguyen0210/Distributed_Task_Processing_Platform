import argparse

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.queue.redis_queue import RedisQueue
from app.workers.worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a taskq worker process.")
    parser.add_argument("--concurrency", type=int, default=1, help="jobs run at once (threads)")
    parser.add_argument(
        "--id", dest="worker_id", default=None, help="worker name (default: random)"
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    queue = RedisQueue.from_settings(settings)
    try:
        Worker(queue, settings, worker_id=args.worker_id, concurrency=args.concurrency).run()
    finally:
        queue.close()


if __name__ == "__main__":
    main()
