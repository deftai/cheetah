"""One transcript job, from a validated `Job` to a rendered transcript.

Pure orchestration over a `CaptionSource`, the cache and the rate limiter, run
in a worker thread (it sleeps). It raises `TranscriptError` for every failure
the worker reports; the consumer turns anything else into `internal`.

Order of work: an earlier identical request is answered from the cache without
asking YouTube; otherwise the tracks are listed, one is chosen, and an entry for
that track is reused if there is one; otherwise its captions are fetched, parsed
and rendered, and the result is cached. Each request to YouTube takes a token
from the worker's bucket first. A request refused for rate limiting or timing
out is retried a bounded number of times with jittered backoff (never sooner
than a Retry-After, never past the job's deadline) - except a machine-translated
track, which YouTube refuses far more often than a native one and which is not
worth hammering: the error then says to ask for the original language.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from . import errors
from .cache import CacheEntry, TranscriptCache
from .captions import parse_vtt
from .ratelimit import TokenBucket, backoff_delay
from .render import MAX_TITLE_CHARS, one_line, render_transcript
from .source import CaptionSource
from .tracks import MANUAL, TRANSLATED, select_track
from .validators import Job

T = TypeVar("T")
_RETRYABLE = frozenset({"rate_limited", "timeout"})
_TITLE_READ_LIMIT = 2000


class TranscriptService:
    def __init__(
        self,
        source: CaptionSource,
        cache: TranscriptCache,
        bucket: TokenBucket,
        *,
        max_attempts: int,
        job_timeout: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._source = source
        self._cache = cache
        self._bucket = bucket
        self._max_attempts = max_attempts
        self._job_timeout = job_timeout
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter

    def _request(self, call: Callable[[], T], deadline: float, *, retry: bool) -> T:
        """One request to YouTube: wait for a token, make the call, retry what is worth retrying."""
        for attempt in range(self._max_attempts):
            wait = self._bucket.reserve(max_wait=deadline - self._clock())
            if wait is None:
                raise errors.timeout()
            self._sleep(wait)
            if self._clock() >= deadline:  # the sleep itself can overshoot
                raise errors.timeout()
            try:
                return call()
            except errors.TranscriptError as e:
                if not retry or e.code not in _RETRYABLE or attempt + 1 >= self._max_attempts:
                    raise
                delay = backoff_delay(attempt, retry_after=e.retry_after, jitter=self._jitter)
                if self._clock() + delay >= deadline:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable: max_attempts is at least 1")

    def run(self, job: Job) -> CacheEntry:
        deadline = self._clock() + self._job_timeout
        video_id = job.video_id

        if (hit := self._cache.lookup_requested(video_id, job.language)) is not None:
            return hit

        info = self._request(lambda: self._source.list_tracks(video_id), deadline, retry=True)
        selection = select_track(info.listing, job.language)
        track = selection.track

        if (hit := self._cache.lookup_track(video_id, track)) is not None:
            self._cache.link(video_id, job.language, track)
            return hit

        try:
            data = self._request(
                lambda: self._source.fetch_captions(info, track), deadline, retry=track.kind != TRANSLATED
            )
        except errors.TranscriptError as e:
            if e.code == "rate_limited":
                raise errors.rate_limited(
                    translated=track.kind == TRANSLATED, original_language=info.listing.original_language_hint()
                ) from e
            raise

        cues = parse_vtt(data, rolling_dedupe=track.kind != MANUAL)
        # yt-dlp's title is third-party data: it may be missing or not a string.
        # (A real title is at most 100 characters; the slice only keeps a hostile one from being copied whole.)
        title = info.title[:_TITLE_READ_LIMIT] if isinstance(info.title, str) else ""
        markdown = render_transcript(
            title=title, video_id=video_id, kind=track.kind, language=track.language, cues=cues
        )
        entry = CacheEntry(
            markdown=markdown,
            title=one_line(title.encode("utf-8", "replace").decode("utf-8")[:MAX_TITLE_CHARS]).strip(),
            transcript=selection.transcript_meta(),
        )
        self._cache.store(video_id, job.language, track, entry)
        return entry

