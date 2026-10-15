"""Entry point: `python -m transcript_worker`.

Wires the pieces and runs the consumer until SIGTERM or SIGINT, then lets the
running jobs finish. Starting the worker is the operator's act (it sits behind a
compose profile); the gateway's TRANSCRIPT_WORKER_ENABLED is a separate switch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import redis
import redis.asyncio

from .cache import RedisStore, TranscriptCache
from .config import Config, ConfigError
from .consumer import Consumer
from .handler import JobHandler
from .pipeline import TranscriptService
from .ratelimit import TokenBucket
from .source import CaptionSource
from .ytdlp_source import YtDlpSource

# ipaddress.is_global differs between 3.12 releases (validators.py keeps its own table for the
# special-purpose ranges, but 3.12.4 is the first release whose answers it was checked against).
MIN_PYTHON = (3, 12, 4)

log = logging.getLogger("transcript_worker")


async def run(
    cfg: Config,
    *,
    source: CaptionSource | None = None,
    aio_client: Any = None,
    cache_client: Any = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Runs the worker until `stop` is set (by SIGTERM or SIGINT when not given). The keyword arguments are
    for tests: a fake source and Redis clients."""
    # The socket timeout must exceed the consumer's BLOCK time (a few seconds).
    aio = aio_client or redis.asyncio.Redis.from_url(
        cfg.client_url, protocol=2, socket_timeout=15, socket_connect_timeout=5, health_check_interval=30
    )
    cache_client = cache_client or redis.Redis.from_url(
        cfg.client_url, protocol=2, socket_timeout=5, socket_connect_timeout=5
    )
    service = TranscriptService(
        source or YtDlpSource(),
        TranscriptCache(RedisStore(cache_client), cfg.cache_ttl),
        TokenBucket(1.0 / cfg.rate_interval, burst=1),
        max_attempts=cfg.max_attempts,
        job_timeout=cfg.job_timeout,
    )
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    log.info(
        "starting: concurrency=%d rate_interval=%gs cache_ttl=%ds consumer=%s",
        cfg.concurrency, cfg.rate_interval, cfg.cache_ttl, cfg.consumer_name,
    )
    with ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="transcript") as pool:
        try:
            await Consumer(aio, cfg, JobHandler(service), executor=pool).run(stop)
        finally:
            await aio.aclose()
            cache_client.close()
    log.info("stopped")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if sys.version_info < MIN_PYTHON:
        log.error("Python %d.%d.%d or newer is required", *MIN_PYTHON)
        return 2
    try:
        cfg = Config.from_env(os.environ, hostname=socket.gethostname())
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2
    asyncio.run(run(cfg))
    return 0
