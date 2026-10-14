"""WebVTT caption parsing.

Caption files are third-party input: the size is checked before parsing, tags
are stripped, entities are decoded after that (so `&lt;b&gt;` stays literal
text), and anything that does not start like a WebVTT file is refused, which
catches an HTML error page served in place of captions.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from . import errors

MAX_CAPTION_BYTES = 5 * 1024 * 1024

_TIMING = re.compile(
    r"(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,2}):(?P<s>\d{2})[.,](?P<ms>\d{3})[ \t]+-->[ \t]+\S"
)
# `[^<>]*` stops a scan at the next `<`, so a long run of `<` without a `>` costs
# one pass overall. With `[^>]*` each `<` would rescan to the end: quadratic on a
# 5 MiB caption file.
_TAG = re.compile(r"<[^<>]*>")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Cue:
    start: float  # seconds
    text: str  # one line


def _clean_line(line: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub("", line))).strip()


def parse_vtt(data: bytes, *, rolling_dedupe: bool = False, max_bytes: int = MAX_CAPTION_BYTES) -> list[Cue]:
    """Parses WebVTT into cues with a start time and one line of text.

    YouTube's auto captions "roll": each cue repeats the previous cue's last
    line before the new one. `rolling_dedupe` drops lines already shown in the
    previous cue. It is for auto tracks only: in manual captions the same line
    can legitimately follow itself ("No." "No.").

    Raises TranscriptError(internal) for an oversize, unreadable or empty file.
    """
    if len(data) > max_bytes:
        raise errors.internal("caption_too_large")
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("WEBVTT"):
        raise errors.internal("caption_unparseable")

    cues: list[Cue] = []
    previous: set[str] = set()
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.split("\n")
        timing_at = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_at is None:
            continue  # header, NOTE, STYLE or REGION block
        m = _TIMING.match(lines[timing_at].strip())
        if m is None:
            continue
        start = int(m["h"] or 0) * 3600 + int(m["m"]) * 60 + int(m["s"]) + int(m["ms"]) / 1000
        body = [c for c in (_clean_line(ln) for ln in lines[timing_at + 1 :]) if c]
        if not body:
            continue
        # A set: `in <list>` would make two cues of n lines each cost O(n^2).
        shown = [ln for ln in body if ln not in previous] if rolling_dedupe else body
        previous = set(body)
        if shown:
            cues.append(Cue(start=start, text=" ".join(shown)))
    if not cues:
        raise errors.internal("caption_empty")
    return cues
