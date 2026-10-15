import pytest

from transcript_worker import errors
from transcript_worker.cache import MemoryStore, TranscriptCache
from transcript_worker.pipeline import TranscriptService
from transcript_worker.ratelimit import TokenBucket
from transcript_worker.source import VideoInfo
from transcript_worker.tracks import Track, TrackListing
from transcript_worker.validators import Job

VID = "dQw4w9WgXcQ"
JOB_ID = "0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20"
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello world\n\n00:00:20.000 --> 00:00:22.000\nsecond line\n"
ROLLING = (
    b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello world\n\n"
    b"00:00:03.000 --> 00:00:05.000\nhello world\nnext words\n"
)


def job(language=None):
    return Job(job_id=JOB_ID, video_id=VID, url=f"https://www.youtube.com/watch?v={VID}", language=language, resolved_ip=None)


def listing(*tracks, spoken=None):
    return TrackListing(tracks=tuple(Track(*t) for t in tracks), spoken_language=spoken)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class FakeSource:
    def __init__(self, tracks, *, title="A video", captions=VTT, list_errors=(), fetch_errors=()):
        self.info = VideoInfo(title=title, listing=tracks)
        self.captions = captions
        self.list_errors = list(list_errors)
        self.fetch_errors = list(fetch_errors)
        self.list_calls = 0
        self.fetch_calls = []

    def list_tracks(self, video_id):
        self.list_calls += 1
        if self.list_errors:
            raise self.list_errors.pop(0)
        return self.info

    def fetch_captions(self, info, track):
        self.fetch_calls.append(track)
        if self.fetch_errors:
            raise self.fetch_errors.pop(0)
        return self.captions


def service(source, *, ttl=60, attempts=3, timeout=90.0, interval=3.0, clock=None, store=None):
    clock = clock or Clock()
    cache = TranscriptCache(store if store is not None else MemoryStore(), ttl)
    bucket = TokenBucket(1 / interval, burst=1, clock=clock.time)
    svc = TranscriptService(
        source, cache, bucket, max_attempts=attempts, job_timeout=timeout,
        clock=clock.time, sleep=clock.sleep, jitter=lambda: 0.5,
    )
    return svc, clock


def rate_limited(retry_after=None):
    return errors.TranscriptError("rate_limited", "slow", retry_after=retry_after)


def test_a_manual_track_becomes_a_transcript():
    src = FakeSource(listing(("en", "manual"), ("fr", "manual")))
    svc, _ = service(src)
    out = svc.run(job("en"))
    assert "[00:01] hello world" in out.markdown and out.markdown.startswith("# A video\n")
    assert out.title == "A video"
    assert out.transcript == {"kind": "manual", "language": "en", "available_languages": ["en", "fr"]}
    assert (src.list_calls, len(src.fetch_calls)) == (1, 1)


def test_a_repeat_request_is_served_from_the_cache_without_touching_youtube():
    src = FakeSource(listing(("en", "manual")))
    svc, _ = service(src)
    first = svc.run(job("en"))
    second = svc.run(job("en"))
    assert second == first
    assert (src.list_calls, len(src.fetch_calls)) == (1, 1)


def test_another_request_for_the_same_track_skips_the_download_and_is_linked():
    src = FakeSource(listing(("en", "manual")))
    svc, _ = service(src)
    svc.run(job("en"))
    svc.run(job(None))  # resolves to the same track after a listing, no download
    assert (src.list_calls, len(src.fetch_calls)) == (2, 1)
    svc.run(job(None))  # now answered from the pointer alone
    assert (src.list_calls, len(src.fetch_calls)) == (2, 1)


def test_the_cache_can_be_turned_off():
    src = FakeSource(listing(("en", "manual")))
    svc, _ = service(src, ttl=0)
    svc.run(job("en"))
    svc.run(job("en"))
    assert (src.list_calls, len(src.fetch_calls)) == (2, 2)


def test_failures_are_never_cached():
    src = FakeSource(listing(("en", "manual")), fetch_errors=[errors.blocked()])
    svc, _ = service(src)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "blocked"
    assert svc.run(job("en")).markdown  # the next job really fetches
    assert len(src.fetch_calls) == 2


def test_no_captions_and_wrong_language_are_reported_before_any_download():
    svc, _ = service(FakeSource(listing()))
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "no_captions"
    src = FakeSource(listing(("fr", "manual")))
    svc, _ = service(src)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "no_track_in_language" and "fr" in e.value.message
    assert src.fetch_calls == []


def test_auto_captions_drop_rolled_over_lines_but_manual_ones_do_not():
    auto, _ = service(FakeSource(listing(("en", "auto")), captions=ROLLING))
    assert auto.run(job("en")).markdown.count("hello world") == 1
    manual, _ = service(FakeSource(listing(("en", "manual")), captions=ROLLING))
    assert manual.run(job("en")).markdown.count("hello world") == 2


@pytest.mark.parametrize("title", [None, 5, b"bytes", ["x"], ""])
def test_a_title_that_is_not_text_becomes_empty(title):
    out = service(FakeSource(listing(("en", "manual")), title=title))[0].run(job("en"))
    assert out.title == "" and out.markdown.startswith("# YouTube video\n")


def test_the_result_title_is_one_line_and_bounded():
    out = service(FakeSource(listing(("en", "manual")), title="a\nb\u2028c" + "x" * 500))[0].run(job("en"))
    assert "\n" not in out.title and "\u2028" not in out.title and len(out.title) <= 300


def test_a_lone_surrogate_in_the_title_cannot_break_json_or_the_cache():
    store = MemoryStore()
    svc, _ = service(FakeSource(listing(("en", "manual")), title="a\ud800b"), store=store)
    out = svc.run(job("en"))
    assert out.title == "a?b"
    out.to_bytes().decode("utf-8")  # encodes without raising
    assert any(":track:" in k for k in store.data), "the entry was cached despite the odd title"


def test_a_native_track_refused_with_429_is_retried_honouring_retry_after():
    src = FakeSource(listing(("en", "auto")), fetch_errors=[rate_limited(40.0), rate_limited()])
    svc, clock = service(src)
    assert svc.run(job("en")).markdown
    assert len(src.fetch_calls) == 3
    assert max(clock.sleeps) >= 40.0, "Retry-After is a lower bound on the wait"


def test_retries_are_bounded_and_the_final_error_is_the_workers_own():
    src = FakeSource(listing(("en", "auto")), fetch_errors=[rate_limited()] * 10)
    svc, _ = service(src, attempts=3)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "rate_limited" and len(src.fetch_calls) == 3
    assert "original language" not in e.value.message


def test_a_refused_translated_track_is_not_retried_and_points_at_the_original():
    src = FakeSource(
        listing(("ko-orig", "auto"), ("en", "translated")), fetch_errors=[rate_limited()] * 5,
    )
    svc, _ = service(src)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert len(src.fetch_calls) == 1
    assert e.value.code == "rate_limited" and "request the original language (ko-orig)" in e.value.message


def test_listing_failures_are_retried_the_same_way():
    src = FakeSource(listing(("en", "manual")), list_errors=[rate_limited(), errors.timeout()])
    svc, _ = service(src)
    assert svc.run(job("en")).markdown
    assert src.list_calls == 3


@pytest.mark.parametrize("make", [errors.blocked, errors.unavailable, errors.no_captions, lambda: errors.internal()])
def test_other_failures_are_not_retried(make):
    src = FakeSource(listing(("en", "manual")), list_errors=[make()])
    svc, clock = service(src)
    with pytest.raises(errors.TranscriptError):
        svc.run(job("en"))
    assert src.list_calls == 1


def test_a_wait_that_would_pass_the_deadline_gives_up_with_the_last_error():
    src = FakeSource(listing(("en", "auto")), fetch_errors=[rate_limited(300.0)] * 3)
    svc, clock = service(src, timeout=30.0)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "rate_limited"
    assert len(src.fetch_calls) == 1 and all(s < 30 for s in clock.sleeps)


def test_every_youtube_request_waits_for_the_rate_limiter():
    src = FakeSource(listing(("en", "manual")))
    svc, clock = service(src, interval=3.0)
    svc.run(job("en"))
    # A burst of one: the listing goes out at once, the caption download waits one interval.
    assert clock.sleeps[0] == 0.0 and clock.sleeps[1] == pytest.approx(3.0)


def test_a_rate_limiter_wait_beyond_the_deadline_is_a_timeout():
    src = FakeSource(listing(("en", "manual")))
    svc, _ = service(src, interval=60.0, timeout=30.0)
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "timeout"
    assert (src.list_calls, src.fetch_calls) == (1, [])
    # The limiter is still drained: the next job cannot even send its first request in time.
    with pytest.raises(errors.TranscriptError) as e:
        svc.run(job("en"))
    assert e.value.code == "timeout" and src.list_calls == 1


def test_an_oversize_or_unreadable_caption_is_an_internal_error():
    big = FakeSource(listing(("en", "manual")), captions=b"WEBVTT\n" + b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(errors.TranscriptError) as e:
        service(big)[0].run(job("en"))
    assert e.value.code == "internal"
    html = FakeSource(listing(("en", "manual")), captions=b"<html>blocked</html>")
    with pytest.raises(errors.TranscriptError) as e:
        service(html)[0].run(job("en"))
    assert e.value.code == "internal"


def test_a_huge_title_is_cut_before_it_is_processed():
    out = service(FakeSource(listing(("en", "manual")), title="t" * 10_000_000))[0].run(job("en"))
    assert len(out.title) <= 300 and len(out.markdown) < 5000
