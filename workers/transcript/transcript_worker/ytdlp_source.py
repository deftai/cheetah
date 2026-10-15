"""The yt-dlp adapter: a `CaptionSource` on top of yt-dlp used as a library.

No subprocess and no shell, so there is no command line to inject into: the
canonical URL is a Python string handed to `extract_info`, and nothing here
builds an argument vector. (A language never reaches yt-dlp at all: the worker
picks the track itself from the info dict and downloads that one URL.) Options
are set to the minimum the job needs: no download, no cookies, no yt-dlp cache,
only the YouTube extractor, no yt-dlp-side retries (each retry must take a rate
limiter token, so the pipeline owns them), and a per-call temp directory for
anything yt-dlp might still write.

What is not limited here: yt-dlp contacts other Google hosts (the player API,
caption hosts) and follows redirects, so the deployment needs its own egress
policy (see the README).

Every yt-dlp exception is turned into a `TranscriptError` from the worker's
fixed vocabulary by `classify_error`; yt-dlp's text never reaches a result.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from typing import Any
from urllib.parse import urlsplit

from . import errors
from .captions import MAX_CAPTION_BYTES
from .ratelimit import parse_retry_after
from .source import VideoInfo
from .tracks import MANUAL, Track, tracks_from_info
from .validators import canonical_url

log = logging.getLogger(__name__)

SOCKET_TIMEOUT = 20
_READ_PIECE = 64 * 1024
_MAX_CHAIN = 8

# Phrases in yt-dlp's (English) messages. They only pick a code; none is ever echoed.
_BLOCKED = ("not a bot", "unusual traffic", "captcha")
_UNAVAILABLE = (
    "video unavailable", "this video is unavailable", "this video is not available", "private video",
    "this video is private", "has been removed", "removed by the uploader", "has been terminated",
    "members-only", "members only", "available to this channel's members", "join this channel",
    "confirm your age", "age-restricted", "in your country", "blocked it", "no longer available",
    "premieres in", "live event will begin", "this live event",
)
_TIMEOUT = ("timed out", "timeout")


def ydl_options(work_dir: str) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "skip_download": True,
        "noplaylist": True,
        "allowed_extractors": ["youtube"],
        "cachedir": False,
        "geo_bypass": False,  # the default adds a spoofed X-Forwarded-For header
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "file_access_retries": 0,
        "ignore_no_formats_error": True,  # only captions are wanted
        "check_formats": False,
        # The DASH and HLS manifests are separate requests to YouTube and not needed for captions.
        "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
        "paths": {"home": work_dir, "temp": work_dir},
        "outtmpl": {"default": f"{work_dir.replace('%', '%%')}/%(id)s.%(ext)s"},  # a % in the path is not a template field
    }


def _loggable(e: BaseException) -> str:
    """What an operator needs to diagnose a yt-dlp failure: the class and a short, printable message. It
    goes to the log only, never into a result."""
    text = re.sub(r"https?://\S+", "<url>", str(e))  # signed caption URLs carry short-lived parameters
    return f"{type(e).__name__}: {re.sub(r'[^ -~]', '?', text)[:300]}"


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and what led to it: `__cause__`, `__context__` and yt-dlp's wrapped `exc_info`."""
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo and len(seen) < _MAX_CHAIN:
        e = todo.pop(0)
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        info = getattr(e, "exc_info", None)
        inner = info[1] if isinstance(info, tuple) and len(info) > 1 else None
        todo += [e.__cause__, e.__context__, inner if isinstance(inner, BaseException) else None]


def classify_error(exc: BaseException) -> errors.TranscriptError:
    """Maps whatever yt-dlp (or the network) raised to the worker's error vocabulary."""
    if isinstance(exc, errors.TranscriptError):
        return exc
    chain = list(_chain(exc))
    text = " ".join(str(e) for e in chain)[:2000].casefold()
    statuses = {s for e in chain if isinstance(s := getattr(e, "status", None), int)}
    if 429 in statuses or "http error 429" in text or "too many requests" in text:
        retry_after = None
        for e in chain:
            headers = getattr(getattr(e, "response", None), "headers", None)
            if headers is not None and (retry_after := parse_retry_after(headers.get("Retry-After"))) is not None:
                break
        err = errors.rate_limited(translated=False, original_language=None)
        err.retry_after = retry_after
        return err
    if 403 in statuses or "http error 403" in text or any(m in text for m in _BLOCKED):
        return errors.blocked()
    if any(m in text for m in _UNAVAILABLE):
        return errors.unavailable()
    if (
        statuses & {408, 504}
        or any(isinstance(e, TimeoutError) for e in chain)
        or any(m in text for m in _TIMEOUT)
    ):
        return errors.timeout()
    return errors.internal("unexpected")


def _caption_url_ok(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and "%" not in parts.netloc  # a percent-encoded host is decoded later by the HTTP stack
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and (host == "youtube.com" or host.endswith(".youtube.com"))
        )
    except ValueError:
        return False


def _vtt_urls(info: Mapping[str, object]) -> dict[tuple[str, str], str]:
    """(group, yt-dlp key) -> caption URL, for the WebVTT format of each track whose URL is safe to fetch.
    The group is "manual" for `subtitles` and "auto" for `automatic_captions` (native and translated)."""
    out: dict[tuple[str, str], str] = {}
    for group, field in (("manual", "subtitles"), ("auto", "automatic_captions")):
        tracks = info.get(field)
        if not isinstance(tracks, Mapping):
            continue
        for key, formats in tracks.items():
            if not isinstance(key, str) or not isinstance(formats, list):
                continue
            for f in formats:
                if isinstance(f, Mapping) and f.get("ext") == "vtt" and _caption_url_ok(f.get("url")):
                    out[(group, key)] = f["url"]
                    break
    return out


YdlFactory = Callable[[dict[str, Any]], AbstractContextManager[Any]]


def _default_factory(params: dict[str, Any]) -> AbstractContextManager[Any]:
    import yt_dlp  # imported here so the rest of the package (and its tests) load without it

    return yt_dlp.YoutubeDL(params)  # type: ignore[no-any-return]


class YtDlpSource:
    def __init__(self, ydl_factory: YdlFactory = _default_factory, work_root: str | None = None) -> None:
        self._factory = ydl_factory
        self._work_root = work_root

    def _session(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix="transcript-", dir=self._work_root)

    def list_tracks(self, video_id: str) -> VideoInfo:
        with self._session() as work:
            try:
                with self._factory(ydl_options(work)) as ydl:
                    info = ydl.extract_info(canonical_url(video_id), download=False)
            except Exception as e:
                log.warning("yt-dlp could not list tracks for %s: %s", video_id, _loggable(e))
                raise classify_error(e) from None
        if not isinstance(info, Mapping) or info.get("_type", "video") != "video":
            raise errors.internal("unexpected")
        return VideoInfo(title=info.get("title"), listing=tracks_from_info(info), handle=_vtt_urls(info))

    def fetch_captions(self, info: VideoInfo, track: Track) -> bytes:
        handle = info.handle if isinstance(info.handle, dict) else {}
        url = handle.get(("manual" if track.kind == MANUAL else "auto", track.key))
        if url is None:
            raise errors.internal("caption_unparseable")
        from yt_dlp.networking import Request  # deferred with the factory's import

        with self._session() as work:
            try:
                with self._factory(ydl_options(work)) as ydl:
                    response = ydl.urlopen(Request(url))
                    try:
                        return _read_bounded(response)
                    finally:
                        response.close()
            except Exception as e:
                log.warning("caption download failed: %s", _loggable(e))
                raise classify_error(e) from None


def _read_bounded(response: Any) -> bytes:
    """At most MAX_CAPTION_BYTES + 1 bytes: enough for the parser to see that the file is too big, without
    holding the rest. A read may return less than asked for, so it goes on until the limit or the end."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_CAPTION_BYTES:
        chunk = response.read(min(_READ_PIECE, MAX_CAPTION_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)
