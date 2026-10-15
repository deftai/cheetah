"""The transcript cache.

Only successes are cached. An entry is the rendered transcript plus what the
result needs besides it, stored under video id + the track that was chosen, so
two requests that resolve to the same track share one entry. Which track a
request resolves to is only known after YouTube has listed the tracks, so a
small pointer (video id + requested language -> that key) lets a repeat request
skip YouTube entirely.

The cache holds third-party caption text: TTL is an operator setting
(`TRANSCRIPT_CACHE_TTL_SECONDS`, 0 turns it off). Redis is not trusted to hold
only what was written here: an entry that fails any check, or a pointer that
leads anywhere but to a transcript entry of the same video, is a miss. Redis
being down is a miss too, never a job failure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .render import MAX_OUTPUT_BYTES, MAX_TITLE_CHARS
from .tracks import AUTO, MANUAL, TRANSLATED, Track
from .validators import MAX_AVAILABLE_LANGUAGES, valid_language_tag, valid_request_language, valid_video_id

log = logging.getLogger(__name__)

PREFIX = "transcript_cache:v1:"
_KINDS = (MANUAL, AUTO, TRANSLATED)
# Pointer values are keys, so an upper bound keeps a hostile value from being read at length.
_MAX_POINTER_BYTES = 128


class CacheStore(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def set(self, key: str, value: bytes, ttl: int) -> None: ...


class MemoryStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: bytes, ttl: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl


class RedisStore:
    """Cache store on a synchronous redis-py client (the pipeline runs in worker threads)."""

    def __init__(self, client: object) -> None:
        self._client = client

    def get(self, key: str) -> bytes | None:
        value = self._client.get(key)  # type: ignore[attr-defined]
        return value if isinstance(value, bytes) else None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        self._client.set(key, value, ex=ttl)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class CacheEntry:
    markdown: str
    title: str
    transcript: Mapping[str, object]  # the TranscriptMeta: kind, language, available_languages

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"v": 1, "markdown": self.markdown, "title": self.title, "transcript": dict(self.transcript)},
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> CacheEntry | None:
        try:
            d = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("v") != 1:
            return None
        md, title, tr = d.get("markdown"), d.get("title"), d.get("transcript")
        if not isinstance(md, str) or not md or len(md.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
            return None
        if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS or not isinstance(tr, dict):
            return None
        langs = tr.get("available_languages", [])
        if (
            tr.get("kind") not in _KINDS
            or not valid_language_tag(tr.get("language"))
            or not isinstance(langs, list)
            or len(langs) > MAX_AVAILABLE_LANGUAGES
            or not all(valid_language_tag(x) for x in langs)
        ):
            return None
        return cls(markdown=md, title=title, transcript={"kind": tr["kind"], "language": tr["language"], "available_languages": langs})


def _check(video_id: str, requested: str | None) -> None:
    if not valid_video_id(video_id) or (requested is not None and not valid_request_language(requested)):
        raise ValueError("invalid cache key part")


class TranscriptCache:
    def __init__(self, store: CacheStore, ttl_seconds: int) -> None:
        self._store = store
        self._ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    @staticmethod
    def _track_key(video_id: str, track: Track) -> str:
        if track.kind not in _KINDS:
            raise ValueError("invalid track kind")
        return f"{PREFIX}track:{video_id}:{track.kind}:{track.key}"

    @staticmethod
    def _request_key(video_id: str, requested: str | None) -> str:
        return f"{PREFIX}req:{video_id}:{requested.casefold() if requested else '-'}"

    def _get(self, key: str) -> bytes | None:
        try:
            return self._store.get(key)
        except Exception as e:  # a cache that cannot be read is a miss
            log.warning("cache read failed: %s", type(e).__name__)
            return None

    def lookup_track(self, video_id: str, track: Track) -> CacheEntry | None:
        _check(video_id, None)
        key = self._track_key(video_id, track)
        if not self.enabled:
            return None
        raw = self._get(key)
        return CacheEntry.from_bytes(raw) if raw is not None else None

    def lookup_requested(self, video_id: str, requested: str | None) -> CacheEntry | None:
        """The entry an earlier identical request resolved to, without asking YouTube."""
        _check(video_id, requested)
        if not self.enabled:
            return None
        ptr = self._get(self._request_key(video_id, requested))
        if ptr is None or len(ptr) > _MAX_POINTER_BYTES:
            return None
        try:
            key = ptr.decode("ascii")
        except UnicodeDecodeError:
            return None
        if not key.startswith(f"{PREFIX}track:{video_id}:"):
            return None
        raw = self._get(key)
        return CacheEntry.from_bytes(raw) if raw is not None else None

    def link(self, video_id: str, requested: str | None, track: Track) -> None:
        """Points a request at an entry that already exists, without renewing the entry's TTL."""
        _check(video_id, requested)
        key = self._track_key(video_id, track)
        if not self.enabled:
            return
        try:
            self._store.set(self._request_key(video_id, requested), key.encode("ascii"), self._ttl)
        except Exception as e:
            log.warning("cache write failed: %s", type(e).__name__)

    def store(self, video_id: str, requested: str | None, track: Track, entry: CacheEntry) -> None:
        _check(video_id, requested)
        key = self._track_key(video_id, track)
        if not self.enabled:
            return
        try:
            self._store.set(key, entry.to_bytes(), self._ttl)
            self._store.set(self._request_key(video_id, requested), key.encode("ascii"), self._ttl)
        except Exception as e:  # losing a cache write costs one later fetch
            log.warning("cache write failed: %s", type(e).__name__)
