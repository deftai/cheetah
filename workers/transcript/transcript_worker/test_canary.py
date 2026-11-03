import json
import logging

import pytest

from transcript_worker import canary, errors
from transcript_worker.source import VideoInfo
from transcript_worker.tracks import Track, TrackListing

VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello world\n\n00:00:20.000 --> 00:00:22.000\nsecond line\n"
LISTING = TrackListing(tracks=(Track("en", "manual"),))


class FakeSource:
    def __init__(self, *, title="A video", listing=LISTING, captions=VTT, list_error=None, fetch_error=None):
        self.info = VideoInfo(title=title, listing=listing)
        self.captions = captions
        self.list_error = list_error
        self.fetch_error = fetch_error
        self.listed = []
        self.fetched = 0

    def list_tracks(self, video_id):
        self.listed.append(video_id)
        if self.list_error:
            raise self.list_error
        return self.info

    def fetch_captions(self, info, track):
        self.fetched += 1
        if self.fetch_error:
            raise self.fetch_error
        return self.captions


def run(source):
    # A tiny interval so the second request does not wait for the 3 s default.
    return canary.check(source, rate_interval=0.001)


def test_a_real_looking_transcript_is_ok_and_asks_for_the_canary_video_in_english():
    src = FakeSource()
    verdict = run(src)
    assert verdict.status == canary.OK and verdict.code is None
    assert src.listed == [canary.VIDEO_ID]


@pytest.mark.parametrize("code", ["blocked", "rate_limited", "timeout", "unavailable"])
def test_a_refusal_from_youtube_is_inconclusive_not_broken(code):
    verdict = run(FakeSource(list_error=errors.TranscriptError(code, "x")))
    assert (verdict.status, verdict.code) == (canary.INCONCLUSIVE, code)


def test_a_refused_caption_download_is_inconclusive_too():
    verdict = run(FakeSource(fetch_error=errors.blocked()))
    assert (verdict.status, verdict.code) == (canary.INCONCLUSIVE, "blocked")


@pytest.mark.parametrize("error", [errors.internal(), errors.no_captions(), errors.no_track_in_language("en", ["fr"])])
def test_an_answer_that_is_wrong_for_a_video_with_captions_is_broken(error):
    verdict = run(FakeSource(list_error=error))
    assert (verdict.status, verdict.code) == (canary.BROKEN, error.code)


def test_a_listing_with_no_tracks_is_broken():
    verdict = run(FakeSource(listing=TrackListing(tracks=())))
    assert (verdict.status, verdict.code) == (canary.BROKEN, "no_captions")


def test_a_caption_file_that_stops_parsing_is_broken():
    verdict = run(FakeSource(captions=b"<html>not webvtt</html>"))
    assert (verdict.status, verdict.code) == (canary.BROKEN, "internal")


def test_an_unexpected_exception_is_broken_and_only_its_type_is_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="transcript_worker.canary"):
        verdict = run(FakeSource(list_error=RuntimeError("secret-from-youtube")))
    assert verdict.status == canary.BROKEN and verdict.code is None
    assert "RuntimeError" in caplog.text and "secret-from-youtube" not in caplog.text + verdict.to_json()


def test_a_refused_request_is_not_retried_so_the_canary_makes_two_operations_at_most():
    listing_refused = FakeSource(list_error=errors.TranscriptError("rate_limited", "x"))
    run(listing_refused)
    assert len(listing_refused.listed) == 1 and listing_refused.fetched == 0
    download_refused = FakeSource(fetch_error=errors.TranscriptError("rate_limited", "x"))
    run(download_refused)
    assert len(download_refused.listed) == 1 and download_refused.fetched == 1


def test_nothing_youtube_sent_reaches_the_verdict():
    verdict = run(FakeSource(title="SECRET-TITLE", captions=VTT.replace(b"hello world", b"SECRET-CUE")))
    assert verdict.status == canary.OK
    assert "SECRET" not in verdict.to_json()
    refused = run(FakeSource(list_error=errors.TranscriptError("blocked", "SECRET-MESSAGE")))
    assert refused.status == canary.INCONCLUSIVE and "SECRET" not in refused.to_json()


def test_the_verdict_is_one_json_line_with_the_yt_dlp_version():
    data = json.loads(run(FakeSource()).to_json())
    assert set(data) == {"status", "detail", "yt_dlp", "code"}
    assert data["yt_dlp"] == canary.yt_dlp_version()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (FakeSource(), 0),
        (FakeSource(list_error=errors.internal()), 1),
        (FakeSource(list_error=errors.blocked()), 2),
    ],
)
def test_the_exit_code_follows_the_verdict(monkeypatch, capsys, source, expected):
    real_check = canary.check
    monkeypatch.setattr(canary, "YtDlpSource", lambda: source)
    monkeypatch.setattr(canary, "check", lambda s: real_check(s, rate_interval=0.001))
    assert canary.main() == expected
    assert json.loads(capsys.readouterr().out)["status"] in canary.EXIT_CODES
