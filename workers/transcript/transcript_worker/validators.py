"""Defence in depth on job input.

The gateway already validates everything below. The worker re-checks it because
a stream entry is whatever a writer to Redis put there, and because a language
or video id ends up in a yt-dlp call. All checks use `re.fullmatch`: in Python
`$` also matches before a trailing newline, which `fullmatch` does not.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

STRATEGY = "youtube_transcript"
MAX_AVAILABLE_LANGUAGES = 100

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# The gateway's transcriptLanguagePattern: a 2-3 letter language with optional
# subtags (en, pt-BR, zh-Hans) and the original-speech form (ko-orig).
_REQUEST_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}")
# Caption language tags read back from YouTube metadata (third-party strings).
_LANGUAGE_TAG = re.compile(r"[A-Za-z0-9-]{1,16}")
# YouTube's playlist and channel-live embed pages: 11 id characters, not videos.
_NOT_VIDEOS = frozenset({"videoseries", "live_stream"})
# Ranges that `ipaddress.is_global` accepts on some Python versions but that are never a
# pin target. IPv6 must also sit inside 2000::/3 (global unicast), which already excludes
# ::/96 (IPv4-compatible), ::ffff:0:0/96 (mapped), 64:ff9b::/96 (NAT64), fec0::/10 and 5f00::/16.
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
_NOT_A_PIN = tuple(
    ipaddress.ip_network(n)
    for n in (
        "192.0.0.0/24",  # IETF protocol assignments (192.0.0.9 and .10 are 'global' by IANA)
        "192.31.196.0/24", "192.52.193.0/24", "192.175.48.0/24",  # AS112 and AMT anycast
        "192.88.99.0/24",  # 6to4 relay anycast
        "2001::/23",  # IETF protocol assignments, Teredo, benchmarking, ORCHID
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4: embeds an IPv4 address
        "3fff::/20",  # documentation (RFC 9637); older is_global tables do not list it
    )
)


class JobRejected(Exception):
    """The job entry is not acceptable. `job_id` is set when it was readable,
    so the worker can still write a failed result for it."""

    def __init__(self, reason: str, job_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.job_id = job_id


@dataclass(frozen=True)
class Job:
    job_id: str
    video_id: str
    url: str
    language: str | None
    resolved_ip: str | None


def valid_video_id(s: object) -> bool:
    return isinstance(s, str) and _VIDEO_ID.fullmatch(s) is not None and s not in _NOT_VIDEOS


def canonical_url(video_id: str) -> str:
    return "https://www.youtube.com/watch?v=" + video_id


def valid_request_language(s: object) -> bool:
    """A language a caller may request: the gateway's pattern, at most 20
    characters, and never "all" (yt-dlp reads it as every subtitle track)."""
    return (
        isinstance(s, str)
        and len(s) <= 20
        and s.casefold() != "all"
        and _REQUEST_LANGUAGE.fullmatch(s) is not None
    )


def valid_language_tag(s: object) -> bool:
    """A caption language tag from YouTube metadata, safe to echo to callers."""
    return isinstance(s, str) and _LANGUAGE_TAG.fullmatch(s) is not None


def clean_languages(tags: Iterable[object], limit: int = MAX_AVAILABLE_LANGUAGES) -> list[str]:
    """Valid, de-duplicated (case-insensitively), sorted tags, at most `limit`."""
    seen: dict[str, str] = {}
    for t in tags:
        if valid_language_tag(t):
            seen.setdefault(str(t).casefold(), str(t))
    return sorted(seen.values(), key=str.casefold)[:limit]


def _is_public_pin(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, text: str) -> bool:
    """A public unicast address, decided here rather than by the interpreter's
    `is_global` table alone, which differs between Python releases."""
    if "%" in text or not addr.is_global or addr.is_multicast:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr not in _GLOBAL_UNICAST_V6:
        return False
    return not any(addr in net for net in _NOT_A_PIN if net.version == addr.version)


def _valid_job_id(s: object) -> bool:
    if not isinstance(s, str) or len(s) != 36:
        return False
    try:
        return str(uuid.UUID(s)) == s.lower()
    except ValueError:
        return False


def validate_job(fields: Mapping[object, object]) -> Job:
    """Checks a stream entry and returns the job, or raises JobRejected.

    Every field must be a `str` (decode before calling). Unknown fields are
    ignored. `language` is optional but, when present, must be valid: an empty
    value is rejected rather than read as "no language". `resolved_ip` is optional
    (empty means absent) but, when present, must be a public unicast address.
    """
    raw_id = fields.get("job_id")
    job_id = raw_id if isinstance(raw_id, str) and _valid_job_id(raw_id) else None

    def reject(reason: str) -> JobRejected:
        return JobRejected(reason, job_id)

    if job_id is None:
        raise reject("job_id is missing or not a UUID")
    if fields.get("strategy") != STRATEGY:
        raise reject("strategy is not youtube_transcript")
    if fields.get("format") not in (None, "markdown"):
        raise reject("format is not markdown")
    video_id = fields.get("video_id")
    if not valid_video_id(video_id):
        raise reject("video_id is not a YouTube video id")
    assert isinstance(video_id, str)
    url = fields.get("url")
    if url != canonical_url(video_id):
        raise reject("url is not the canonical URL for video_id")
    language: str | None = None
    if "language" in fields:
        lang = fields["language"]
        if not valid_request_language(lang):
            raise reject("language is not a valid caption language")
        assert isinstance(lang, str)
        language = lang
    resolved_ip: str | None = None
    ip = fields.get("resolved_ip", "")  # absent or empty means no pin; anything else must be a public IP
    if ip != "":
        # ip_address() also accepts ints, which are not what a stream carries.
        if not isinstance(ip, str):
            raise reject("resolved_ip is not an IP address")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise reject("resolved_ip is not an IP address") from None
        # The gateway pins www.youtube.com to this address. Loopback, private,
        # link-local (cloud metadata), multicast and scoped (`fe80::1%eth0`)
        # addresses are never a valid pin, whoever wrote the stream entry.
        if not _is_public_pin(addr, ip):
            raise reject("resolved_ip is not a public address")
        resolved_ip = ip
    return Job(job_id=job_id, video_id=video_id, url=url, language=language, resolved_ip=resolved_ip)
