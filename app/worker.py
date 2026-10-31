
import logging
import signal
import threading

from redis.exceptions import RedisError

from app.consumer import ensure_group, consume_once


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

stop = threading.Event()

RETRY_WAIT_SECONDS = 2


def handle_shutdown(signum, frame):
    """Request the worker to stop."""
    stop.set()
    logger.info("Shutdown signal received: %s", signum)


def main():
    # register shutdown signals in main thread
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logger.info("Consumer worker starting")

    while not stop.is_set():
        try:
            # Create the consumer group if it does not exist.
            ensure_group()

            # Read and process available messages.
            consume_once()

        except RedisError as exc:
            logger.warning("Redis unavailable: %s", exc)
            stop.wait(RETRY_WAIT_SECONDS)

        except Exception:
            logger.exception("Unexpected worker error")
            stop.wait(RETRY_WAIT_SECONDS)

    logger.info("Consumer worker stopped")


if __name__ == "__main__":
    main()
