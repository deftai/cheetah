"""The seam between the worker and YouTube.

Everything that needs YouTube goes through `CaptionSource`, so the pipeline and
the consumer are tested without it. A source makes one attempt per call and
raises `TranscriptError` (already classified into the worker's vocabulary, with
`retry_after` set when YouTube asked for a pause); retries, rate limiting and
the time limit belong to the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .tracks import Track, TrackListing


@dataclass(frozen=True)
class VideoInfo:
    # Third-party text; `object` because yt-dlp may hand back None or a non-string.
    title: object
    listing: TrackListing
    # Whatever the source needs to fetch a track later (the yt-dlp adapter keeps the caption URLs here).
    handle: object = None


@runtime_checkable
class CaptionSource(Protocol):
    def list_tracks(self, video_id: str) -> VideoInfo:
        """The video's title and caption tracks."""
        ...

    def fetch_captions(self, info: VideoInfo, track: Track) -> bytes:
        """The WebVTT file of one of `info`'s tracks, at most `captions.MAX_CAPTION_BYTES` + 1 bytes."""
        ...
