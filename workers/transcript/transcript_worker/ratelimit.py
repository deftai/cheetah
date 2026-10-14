"""Rate limiting and retry delays. The clock is a parameter, so nothing here
sleeps or reads the wall clock in tests."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime


class TokenBucket:
    """Global limit on caption fetches for one worker process.

    `reserve()` is called from the thread pool, so it is locked. It takes the
    token at once and tells the caller how long to wait before using it; waiting
    callers are therefore spaced out instead of all waking together.
    """

    def __init__(
        self,
        rate_per_second: float,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (rate_per_second > 0 and math.isfinite(rate_per_second)):
            raise ValueError("rate_per_second must be a positive number")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._lock = threading.Lock()

    def reserve(self, max_wait: float | None = None) -> float | None:
        """Takes a token and returns the seconds to wait before using it (0.0 if
        none). If the wait would exceed `max_wait`, takes nothing and returns None."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + max(0.0, now - self._last) * self._rate)
            self._last = now
            wait = 0.0 if self._tokens >= 1 else (1 - self._tokens) / self._rate
            if max_wait is not None and wait > max_wait:
                return None
            self._tokens -= 1
            return wait


MAX_RETRY_AFTER = 300.0


def parse_retry_after(value: object, now_epoch: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or an HTTP date), capped
    at MAX_RETRY_AFTER; None when absent, unreadable or not in the future."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.isascii() and v.isdigit():
        secs = float(int(v)) if len(v) <= 9 else MAX_RETRY_AFTER
    else:
        try:
            when = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            return None
        secs = float(when.timestamp() - (time.time() if now_epoch is None else now_epoch))
    if not (secs > 0):
        return None
    return min(secs, MAX_RETRY_AFTER)


def backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    base: float = 2.0,
    cap: float = 60.0,
    jitter: Callable[[], float] = lambda: 0.5,
) -> float:
    """Seconds to wait before retry number `attempt` (0-based): exponential with
    jitter in [0.5, 1.5) of the step (`jitter()` returns a value in [0, 1)),
    never shorter than a server's Retry-After, and never longer than `cap`
    unless Retry-After itself asks for more (itself bounded by MAX_RETRY_AFTER)."""
    step = min(cap, base * (2.0 ** max(0, min(attempt, 30))))
    delay = step * (0.5 + jitter())
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, max(cap, retry_after or 0.0))
