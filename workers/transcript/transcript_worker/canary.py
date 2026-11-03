"""Is the pinned yt-dlp still able to read YouTube captions? `python -m transcript_worker.canary`

One public video, two operations (the track listing and one caption file), no retry, through the same pipeline a job
takes. The point is to hear about a YouTube change before a user does, so the verdict separates the two
ways this fails:

- exit 0, `ok`: a real transcript came back.
- exit 1, `broken`: the request went through and the answer was wrong (a caption file that no longer
  parses, no captions on a video known to have them, an error the worker does not classify). yt-dlp or the worker needs a change.
- exit 2, `inconclusive`: YouTube refused this network (`blocked`, `rate_limited`), the request timed out,
  or the video looked unavailable. That says something about where the canary ran, not about yt-dlp, so
  one such run is not an alert; several in a row from a network that used to pass is.

The verdict is one JSON line on stdout. It holds only worker-written text and the yt-dlp version, never
anything YouTube sent. (stderr may carry short, scrubbed yt-dlp diagnostics from `ytdlp_source`.) The worker does not run this by itself and nothing in this repository schedules it:
whoever runs the worker decides when, and from which network (see the README, "Keeping yt-dlp working").
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from importlib import metadata

from . import errors
from .cache import MemoryStore, TranscriptCache
from .pipeline import TranscriptService
from .ratelimit import TokenBucket
from .source import CaptionSource
from .validators import Job, canonical_url
from .ytdlp_source import YtDlpSource

OK, BROKEN, INCONCLUSIVE = "ok", "broken", "inconclusive"
EXIT_CODES = {OK: 0, BROKEN: 1, INCONCLUSIVE: 2}

# "Me at the zoo": the first YouTube video, 19 seconds, creator-made English captions (the live test's video).
VIDEO_ID = "jNQXAC9IVRw"
LANGUAGE = "en"
_JOB_ID = "00000000-0000-4000-8000-000000000000"

# Errors that describe the network or the moment, not a change in how YouTube serves captions.
_ENVIRONMENT = frozenset({"blocked", "rate_limited", "timeout", "unavailable"})

log = logging.getLogger("transcript_worker.canary")


@dataclass(frozen=True)
class Verdict:
    status: str
    detail: str
    yt_dlp: str
    code: str | None = None  # the worker error code, when the check ended in one

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def yt_dlp_version() -> str:
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return "unknown"


def check(source: CaptionSource, *, rate_interval: float = 3.0) -> Verdict:
    """Runs one job for the canary video and judges the outcome. Never raises."""
    version = yt_dlp_version()
    service = TranscriptService(
        source,
        TranscriptCache(MemoryStore(), 0),
        TokenBucket(1.0 / rate_interval, burst=1),
        max_attempts=1,  # no retry: one run is the signal, and it stays at two operations (list, download)
        job_timeout=90.0,
    )
    job = Job(job_id=_JOB_ID, video_id=VIDEO_ID, url=canonical_url(VIDEO_ID), language=LANGUAGE, resolved_ip=None)
    try:
        service.run(job)  # raises unless a transcript was rendered; an empty one is an error
    except errors.TranscriptError as e:
        if e.code in _ENVIRONMENT:
            return Verdict(INCONCLUSIVE, f"YouTube did not serve the request ({e.code}); this network or the moment may be at fault; read the README before bumping yt-dlp.", version, e.code)
        return Verdict(BROKEN, f"The canary video failed with {e.code}; yt-dlp or the worker needs a change.", version, e.code)
    except Exception as e:
        # Type only: the message can carry YouTube text or a signed URL (see ytdlp_source._loggable).
        log.error("canary check raised %s", type(e).__name__)
        return Verdict(BROKEN, "The canary check raised an unexpected error; see the log for its type.", version)
    return Verdict(OK, "A real transcript was fetched and rendered.", version)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    verdict = check(YtDlpSource())
    print(verdict.to_json())
    return EXIT_CODES[verdict.status]


if __name__ == "__main__":
    raise SystemExit(main())
