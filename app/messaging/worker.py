import logging
import signal
import threading

from redis import Redis
from redis.exceptions import RedisError

from app.logging_config import setup_logging
from app.messaging.consumer import consume_once, ensure_group, get_consumer_redis

setup_logging()

logger = logging.getLogger(__name__)


RETRY_WAIT_SECONDS = 2


def request_stop(stop: threading.Event, signum: int) -> None:
    """Ask the worker to stop after the batch it is working on."""
    stop.set()
    logger.info("Shutdown signal received: %s", signum)


def main(stop: threading.Event | None = None, redis: Redis | None = None) -> None:
    # The stop flag and the Redis client are created here, or passed in by a test.
    stop = stop if stop is not None else threading.Event()
    redis = redis if redis is not None else get_consumer_redis()

    # register shutdown signals in main thread
    def handle_shutdown(signum, frame):
        request_stop(stop, signum)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logger.info("Consumer worker starting")

    group_ready = False

    while not stop.is_set():
        try:
            # Create the consumer group once, and again after a Redis problem
            # (the group may be gone if Redis restarted without its data).
            if not group_ready:
                ensure_group(redis)
                group_ready = True

            # Read and process available messages.
            consume_once(redis)

        except RedisError as exc:
            group_ready = False
            logger.warning("Redis unavailable: %s", exc)
            stop.wait(RETRY_WAIT_SECONDS)

        except Exception:
            logger.exception("Unexpected worker error")
            stop.wait(RETRY_WAIT_SECONDS)

    logger.info("Consumer worker stopped")


if __name__ == "__main__":
    main()
