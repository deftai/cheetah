"""Worker settings, read once from the environment.

A bad value stops the worker at start with a message naming the variable and
never its value (`REDIS_URL` can carry a password).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

# Fixed by the gateway (queue.go, youtube.go): the worker has to match them, so they are not settings.
STREAM = "transcript_jobs"
GROUP = "transcript-workers"
RESULTS_PREFIX = "job_results"
RESULT_TTL_SECONDS = 3600  # the gateway's job status TTL

# Entries a dead worker left pending are taken over after this long. A job's time limit stays below
# it, so a healthy replica's job is not claimed by another replica.
CLAIM_IDLE_SECONDS = 120.0
MAX_JOB_TIMEOUT = 100.0
MAX_CACHE_TTL = 30 * 86400

_CONSUMER_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_NOT_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ConfigError(Exception):
    """A setting is unusable. The message names the variable, not its value."""


@dataclass(frozen=True)
class Config:
    redis_url: str
    consumer_name: str
    concurrency: int = 2
    job_timeout: float = 90.0  # seconds one job may take, retries included
    rate_interval: float = 3.0  # minimum seconds between two requests to YouTube from this worker
    cache_ttl: int = 86400  # seconds; 0 turns the cache off
    max_attempts: int = 3  # tries per YouTube request when it is rate limited or times out

    @property
    def client_url(self) -> str:
        """`redis_url` as a URL redis-py accepts: a bare host:port (which the gateway also takes) gets a scheme."""
        return self.redis_url if "://" in self.redis_url else "redis://" + self.redis_url

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> Config:
        def get(name: str) -> str | None:
            v = env.get(name, "").strip()
            return v or None

        def number(name: str, default: float, low: float, high: float, *, integer: bool) -> float:
            raw = get(name)
            if raw is None:
                return default
            try:
                v = float(int(raw)) if integer else float(raw)
            except ValueError:
                raise ConfigError(f"{name} must be {'an integer' if integer else 'a number'}") from None
            if not (math.isfinite(v) and low <= v <= high):
                raise ConfigError(f"{name} must be between {low:g} and {high:g}")
            return v

        redis_url = get("REDIS_URL") or "redis://127.0.0.1:6379"
        if "://" in redis_url:
            # Without this a stray space or an absurd port surfaces later as a connection error that
            # quotes the URL, password included.
            _check_redis_url(redis_url)

        name = get("TRANSCRIPT_CONSUMER_NAME")
        if name is None:
            name = ("transcript-" + (_NOT_NAME_CHARS.sub("-", hostname).strip("-") or "worker"))[:64]
        elif not _CONSUMER_NAME.fullmatch(name):
            raise ConfigError("TRANSCRIPT_CONSUMER_NAME must be 1-64 characters from A-Z a-z 0-9 . _ -")

        return cls(
            redis_url=redis_url,
            consumer_name=name,
            concurrency=int(number("TRANSCRIPT_CONCURRENCY", 2, 1, 8, integer=True)),
            job_timeout=number("TRANSCRIPT_JOB_TIMEOUT_SECONDS", 90.0, 10, MAX_JOB_TIMEOUT, integer=False),
            rate_interval=number("TRANSCRIPT_RATE_INTERVAL_SECONDS", 3.0, 0.1, 3600, integer=False),
            cache_ttl=int(number("TRANSCRIPT_CACHE_TTL_SECONDS", 86400, 0, MAX_CACHE_TTL, integer=True)),
            max_attempts=int(number("TRANSCRIPT_MAX_ATTEMPTS", 3, 1, 5, integer=True)),
        )


def _check_redis_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port out of range
        ok = parts.scheme in ("redis", "rediss", "unix") and not any(c.isspace() for c in url)
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError("REDIS_URL is not a valid redis URL (redis://, rediss:// or unix://)")
