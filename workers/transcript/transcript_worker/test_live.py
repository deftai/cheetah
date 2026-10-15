"""Runs against real YouTube. Opt in: TRANSCRIPT_LIVE_TEST=1 python -m pytest -m live -v

A few requests for two short public videos, spaced by the worker's own rate limiter. Run it from the
network you intend to deploy to: a pass from a home connection says nothing about a datacenter. Run it
after every yt-dlp bump; it is what notices YouTube changing under the pin.
"""

import os

import pytest

from transcript_worker import errors
from transcript_worker.cache import MemoryStore, TranscriptCache
from transcript_worker.captions import parse_vtt
from transcript_worker.pipeline import TranscriptService
from transcript_worker.ratelimit import TokenBucket
from transcript_worker.validators import Job, canonical_url
from transcript_worker.ytdlp_source import YtDlpSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("TRANSCRIPT_LIVE_TEST") != "1", reason="set TRANSCRIPT_LIVE_TEST=1 to run against real YouTube"),
]

# "Me at the zoo": the first YouTube video, 19 seconds, with creator-made English captions.
ZOO = "jNQXAC9IVRw"
# Big Buck Bunny: no captions at all (as of the slice-6 spike).
NO_CAPTIONS = "aqz-KE-bpKQ"


def job(video_id, language=None):
    return Job(
        job_id="0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20", video_id=video_id, url=canonical_url(video_id), language=language,
        resolved_ip=None,
    )


class CountingSource(YtDlpSource):
    def __init__(self):
        super().__init__()
        self.listed = self.fetched = 0

    def list_tracks(self, video_id):
        self.listed += 1
        return super().list_tracks(video_id)

    def fetch_captions(self, info, track):
        self.fetched += 1
        return super().fetch_captions(info, track)


def service(source, ttl=60):
    return TranscriptService(
        source, TranscriptCache(MemoryStore(), ttl), TokenBucket(1 / 3, burst=1), max_attempts=2, job_timeout=90.0
    )


def test_the_adapter_lists_tracks_and_downloads_real_webvtt():
    src = YtDlpSource()
    info = src.list_tracks(ZOO)
    assert isinstance(info.title, str) and "zoo" in info.title.casefold()
    assert any(t.language == "en" for t in info.listing.tracks), info.listing
    track = next(t for t in info.listing.tracks if t.language == "en")
    data = src.fetch_captions(info, track)
    assert data.startswith(b"WEBVTT")
    assert parse_vtt(data, rolling_dedupe=track.kind != "manual"), "the file holds at least one cue"


def test_a_real_job_end_to_end_then_a_cache_hit_that_never_touches_youtube():
    src = CountingSource()
    svc = service(src)
    first = svc.run(job(ZOO, "en"))
    assert first.markdown.startswith("# ") and first.markdown.count("[00:") >= 3
    assert first.transcript["kind"] in ("manual", "auto") and "en" in first.transcript["available_languages"]
    assert (src.listed, src.fetched) == (1, 1)
    assert svc.run(job(ZOO, "en")) == first
    assert (src.listed, src.fetched) == (1, 1)


def test_a_video_without_captions_is_a_clear_error_not_a_scrape():
    with pytest.raises(errors.TranscriptError) as e:
        service(YtDlpSource()).run(job(NO_CAPTIONS))
    assert e.value.code == "no_captions"


def test_a_language_the_video_does_not_have_names_the_ones_it_has():
    with pytest.raises(errors.TranscriptError) as e:
        service(YtDlpSource()).run(job(ZOO, "tlh"))
    assert e.value.code == "no_track_in_language" and "en" in e.value.message
