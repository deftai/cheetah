import io
import os

import pytest
import yt_dlp
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError

from transcript_worker import errors
from transcript_worker.captions import MAX_CAPTION_BYTES
from transcript_worker.tracks import Track
from transcript_worker.ytdlp_source import YtDlpSource, classify_error, ydl_options

VID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VID}"
CAP = "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=en&fmt=vtt"
INFO = {
    "id": VID,
    "title": "A video",
    "language": "en",
    "subtitles": {
        "en": [{"ext": "json3", "url": CAP.replace("vtt", "json3")}, {"ext": "vtt", "url": CAP}],
        "fr": [{"ext": "srv3", "url": "https://www.youtube.com/api/timedtext?lang=fr&fmt=srv3"}],
    },
    "automatic_captions": {"en": [{"ext": "vtt", "url": CAP + "&auto=1"}]},
}


def http_error(status, **headers):
    return HTTPError(Response(io.BytesIO(b""), "https://www.youtube.com/x", headers, status, "reason"))


def download_error(msg, inner=None):
    return DownloadError(f"ERROR: [youtube] {VID}: {msg}", exc_info=(type(inner), inner, None) if inner else None)


class Body:
    """A response that hands out its bytes in small pieces and counts what was read."""

    def __init__(self, data, piece=1000):
        self.data, self.piece, self.pos, self.closed = data, piece, 0, False

    def read(self, n=-1):
        n = self.piece if n is None or n < 0 else min(n, self.piece)
        chunk = self.data[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeYDL:
    instances = []

    def __init__(self, params, info=INFO, body=b"WEBVTT\n", extract_error=None, open_error=None):
        self.params, self._info, self.body, self.extract_error, self.open_error = params, info, body, extract_error, open_error
        self.extract_calls, self.opened, self.temp_seen, self.response = [], [], None, None
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True, **kw):
        self.temp_seen = os.path.isdir(self.params["paths"]["temp"])
        self.extract_calls.append((url, download, kw))
        if self.extract_error:
            raise self.extract_error
        return self._info

    def urlopen(self, req):
        self.opened.append(req)
        if self.open_error:
            raise self.open_error
        self.response = Body(self.body)
        return self.response


def source(**kw):
    FakeYDL.instances.clear()
    return YtDlpSource(ydl_factory=lambda params: FakeYDL(params, **kw))


# --- options -------------------------------------------------------------------------------------


def test_real_yt_dlp_accepts_the_options_and_loads_only_the_youtube_extractor(tmp_path):
    opts = ydl_options(str(tmp_path))
    with yt_dlp.YoutubeDL(opts) as ydl:
        assert list(ydl._ies) == ["Youtube"]
        assert ydl.cache.enabled is False
        assert ydl.params["socket_timeout"] == 20


def test_options_are_locked_down(tmp_path):
    o = ydl_options(str(tmp_path))
    assert o["skip_download"] is True and o["noplaylist"] is True
    assert o["allowed_extractors"] == ["youtube"]
    assert o["cachedir"] is False
    assert not any(k in o for k in ("cookiefile", "cookiesfrombrowser", "username", "password", "netrc", "proxy"))
    assert o["geo_bypass"] is False
    # Retries belong to the pipeline, where each one takes a rate limiter token.
    assert o["retries"] == 0 and o["extractor_retries"] == 0 and o["fragment_retries"] == 0
    assert o["paths"] == {"home": str(tmp_path), "temp": str(tmp_path)}
    assert o["quiet"] is True and o["no_warnings"] is True
    for k in ("writesubtitles", "writeautomaticsub", "writethumbnail", "writeinfojson", "writedescription"):
        assert not o.get(k), f"{k} would write files or pull more tracks"


# --- list_tracks ---------------------------------------------------------------------------------


def test_list_tracks_asks_for_the_canonical_url_without_downloading():
    src = source()
    info = src.list_tracks(VID)
    (ydl,) = FakeYDL.instances
    assert ydl.extract_calls == [(URL, False, {})]
    assert info.title == "A video"
    assert {(t.language, t.kind) for t in info.listing.tracks} == {("en", "manual"), ("fr", "manual"), ("en", "auto")}
    assert info.listing.spoken_language == "en"


def test_each_call_gets_its_own_temp_dir_that_is_gone_afterwards():
    src = source()
    src.list_tracks(VID)
    src.list_tracks(VID)
    a, b = (y.params["paths"]["temp"] for y in FakeYDL.instances)
    assert a != b and all(y.temp_seen for y in FakeYDL.instances)
    assert not os.path.exists(a) and not os.path.exists(b)


def test_the_temp_dir_is_removed_when_yt_dlp_fails():
    src = source(extract_error=download_error("Video unavailable"))
    with pytest.raises(errors.TranscriptError):
        src.list_tracks(VID)
    assert not os.path.exists(FakeYDL.instances[0].params["paths"]["temp"])


@pytest.mark.parametrize("bad", [None, [], "x", {"_type": "playlist", "entries": []}, {"_type": "url"}])
def test_something_that_is_not_one_video_is_an_error(bad):
    with pytest.raises(errors.TranscriptError) as e:
        source(info=bad).list_tracks(VID)
    assert e.value.code in ("internal", "unavailable")


@pytest.mark.parametrize("title", [None, 5, ["x"], b"x"])
def test_the_title_is_passed_through_for_the_pipeline_to_coerce(title):
    info = source(info={**INFO, "title": title}).list_tracks(VID)
    assert info.title == title


def test_only_https_youtube_urls_are_kept_in_the_handle():
    evil = {
        **INFO,
        "subtitles": {
            "en": [{"ext": "vtt", "url": "https://169.254.169.254/latest/meta-data"}],
            "fr": [{"ext": "vtt", "url": "http://www.youtube.com/api/timedtext"}],
            "de": [{"ext": "vtt", "url": "https://evil.example/captions.vtt"}],
            "es": [{"ext": "vtt", "url": "https://www.youtube.com.evil.example/x"}],
            "it": [{"ext": "vtt", "url": "https://user:pw@www.youtube.com/x"}],
            "pt": [{"ext": "vtt", "url": "https://www.youtube.com:8443/x"}],
            "ja": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?lang=ja"}],
        },
        "automatic_captions": {},
    }
    src = source(info=evil)
    info = src.list_tracks(VID)
    assert src.fetch_captions(info, Track("ja", "manual")) == b"WEBVTT\n"
    for lang in ("en", "fr", "de", "es", "it", "pt"):
        with pytest.raises(errors.TranscriptError) as e:
            src.fetch_captions(info, Track(lang, "manual"))
        assert e.value.code == "internal"
    opened = [r.url for y in FakeYDL.instances for r in y.opened]
    assert opened == ["https://www.youtube.com/api/timedtext?lang=ja"], "nothing else was ever requested"


def test_yt_dlp_errors_are_classified_never_passed_through():
    with pytest.raises(errors.TranscriptError) as e:
        source(extract_error=download_error("Sign in to confirm you\u2019re not a bot")).list_tracks(VID)
    assert e.value.code == "blocked" and "bot" not in e.value.message


# --- fetch_captions ------------------------------------------------------------------------------


def test_fetch_captions_downloads_the_vtt_of_the_chosen_track():
    src = source(body=b"WEBVTT\n\nhello")
    info = src.list_tracks(VID)
    assert src.fetch_captions(info, Track("en", "manual")) == b"WEBVTT\n\nhello"
    ydl = FakeYDL.instances[-1]
    assert [r.url for r in ydl.opened] == [CAP]
    assert ydl.response.closed
    assert src.fetch_captions(info, Track("en", "auto")) == b"WEBVTT\n\nhello"
    assert FakeYDL.instances[-1].opened[0].url == CAP + "&auto=1"


def test_a_translated_track_is_looked_up_in_the_automatic_captions():
    tr = {**INFO, "automatic_captions": {"ko-orig": INFO["subtitles"]["en"], "en": [{"ext": "vtt", "url": CAP + "&tlang=en"}]}}
    src = source(info=tr)
    info = src.list_tracks(VID)
    src.fetch_captions(info, Track("en", "translated"))
    assert FakeYDL.instances[-1].opened[0].url.endswith("&tlang=en")


def test_a_variant_track_is_fetched_by_its_key():
    var = {**INFO, "subtitles": {"en-AbCdEfGhIjK": [{"ext": "vtt", "url": CAP + "&name=x"}]}, "automatic_captions": {}}
    src = source(info=var)
    info = src.list_tracks(VID)
    (t,) = info.listing.tracks
    assert (t.language, t.key) == ("en", "en-AbCdEfGhIjK")
    src.fetch_captions(info, t)
    assert FakeYDL.instances[-1].opened[0].url.endswith("&name=x")


def test_a_track_without_a_vtt_format_cannot_be_fetched():
    src = source()
    info = src.list_tracks(VID)
    with pytest.raises(errors.TranscriptError) as e:
        src.fetch_captions(info, Track("fr", "manual"))
    assert e.value.code == "internal"


def test_the_download_is_bounded_not_read_to_the_end():
    big = b"WEBVTT\n" + b"x" * (50 * 1024 * 1024)
    src = source(body=big)
    info = src.list_tracks(VID)
    data = src.fetch_captions(info, Track("en", "manual"))
    assert len(data) == MAX_CAPTION_BYTES + 1
    assert FakeYDL.instances[-1].response.pos <= MAX_CAPTION_BYTES + 1 + 1000


def test_a_short_read_is_not_mistaken_for_the_end():
    # Responses may return fewer bytes than asked for; the loop must go on until EOF.
    src = source(body=b"WEBVTT\n" + b"y" * 5000)
    info = src.list_tracks(VID)
    assert len(src.fetch_captions(info, Track("en", "manual"))) == 5007


def test_a_429_on_the_download_carries_retry_after():
    src = source(open_error=http_error(429, **{"Retry-After": "12"}))
    info = src.list_tracks(VID)
    with pytest.raises(errors.TranscriptError) as e:
        src.fetch_captions(info, Track("en", "manual"))
    assert e.value.code == "rate_limited" and e.value.retry_after == 12.0


def test_an_unexpected_exception_on_the_download_is_internal():
    src = source(open_error=RuntimeError("secret internal detail"))
    info = src.list_tracks(VID)
    with pytest.raises(errors.TranscriptError) as e:
        src.fetch_captions(info, Track("en", "manual"))
    assert e.value.code == "internal" and "secret" not in e.value.message


# --- classification ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,code",
    [
        (http_error(429), "rate_limited"),
        (download_error("HTTP Error 429: Too Many Requests", http_error(429)), "rate_limited"),
        (DownloadError("ERROR: unable to download video data: HTTP Error 429: Too Many Requests"), "rate_limited"),
        (http_error(403), "blocked"),
        (download_error("Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser"), "blocked"),
        (download_error("Sign in to confirm you're not a bot"), "blocked"),
        (download_error("Video unavailable"), "unavailable"),
        (download_error("Private video. Sign in if you've been granted access to this video"), "unavailable"),
        (download_error("Sign in to confirm your age. This video may be inappropriate for some users."), "unavailable"),
        (download_error("This video is available to this channel's members on level: Gold"), "unavailable"),
        (download_error("The uploader has not made this video available in your country"), "unavailable"),
        (download_error("This video has been removed by the uploader"), "unavailable"),
        (download_error("Premieres in 3 hours"), "unavailable"),
        (TimeoutError(), "timeout"),
        (TransportError("The read operation timed out"), "timeout"),
        (download_error("Read timed out.", TransportError("Read timed out.")), "timeout"),
        (http_error(504), "timeout"),
        (RuntimeError("boom"), "internal"),
        (download_error("something nobody anticipated"), "internal"),
        (KeyError("x"), "internal"),
    ],
    ids=lambda v: repr(v)[:60],
)
def test_classification(exc, code):
    assert classify_error(exc).code == code


def test_classified_messages_never_echo_yt_dlp_text():
    for exc in (
        download_error("Video unavailable <b>alert(1)</b>"),
        RuntimeError("password=hunter2"),
        http_error(429),
    ):
        err = classify_error(exc)
        assert "alert" not in err.message and "hunter2" not in err.message


def test_retry_after_is_read_from_the_cause_chain_and_capped():
    err = classify_error(download_error("HTTP Error 429", http_error(429, **{"Retry-After": "99999"})))
    assert err.code == "rate_limited" and err.retry_after == 300.0
    assert classify_error(http_error(429, **{"Retry-After": "soon"})).retry_after is None
    assert classify_error(http_error(429)).retry_after is None


def test_a_cyclic_exception_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert classify_error(a).code == "internal"


def test_a_transcript_error_passes_through_unchanged():
    e = errors.blocked()
    assert classify_error(e) is e


def test_a_percent_sign_in_the_work_dir_is_not_a_template_field():
    o = ydl_options("/tmp/a%(id)s/b")
    assert o["outtmpl"]["default"] == "/tmp/a%%(id)s/b/%(id)s.%(ext)s"
    assert o["paths"]["temp"] == "/tmp/a%(id)s/b"


def test_a_percent_encoded_host_is_not_a_youtube_host():
    evil = {**INFO, "subtitles": {"en": [{"ext": "vtt", "url": "https://evil.com%40www.youtube.com/api/timedtext"}]}, "automatic_captions": {}}
    src = source(info=evil)
    info = src.list_tracks(VID)
    with pytest.raises(errors.TranscriptError):
        src.fetch_captions(info, Track("en", "manual"))


def test_urls_in_yt_dlp_messages_never_reach_the_log(caplog):
    signed = "https://www.youtube.com/api/timedtext?v=x&signature=SECRETSIG&ip=203.0.113.9"
    src = source(extract_error=download_error(f"Max retries exceeded with url: {signed}"))
    with pytest.raises(errors.TranscriptError):
        src.list_tracks(VID)
    assert "SECRETSIG" not in caplog.text and "203.0.113.9" not in caplog.text and "<url>" in caplog.text


def test_yt_dlp_is_given_a_silent_logger_so_it_cannot_print_unscrubbed_errors():
    logger = ydl_options("/tmp/w")["logger"]
    for level in ("debug", "info", "warning", "error"):
        getattr(logger, level)("ERROR: https://www.youtube.com/x?signature=SECRETSIG \x1b[31m")  # does nothing, raises nothing


def test_real_yt_dlp_prints_nothing_for_a_failed_extraction(tmp_path, capfd):
    opts = {**ydl_options(str(tmp_path)), "proxy": "http://127.0.0.1:1"}  # unreachable: the extraction fails
    with yt_dlp.YoutubeDL(opts) as ydl, pytest.raises(DownloadError):
        ydl.extract_info(URL, download=False)
    out = capfd.readouterr()
    assert out.err == "" and out.out == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com\\.youtube.com/x",  # a backslash: some HTTP stacks end the authority there
        "https://evil.com/.youtube.com",
        "https://www.youtube.com./x",
        "https://www.y\u0430utube.com/x",
        "https://www.youtube.com/x\n",
        "https://-.youtube.com/x",
    ],
)
def test_odd_hosts_and_characters_are_refused(url):
    from transcript_worker.ytdlp_source import _caption_url_ok

    assert not _caption_url_ok(url)
