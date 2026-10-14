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
# No code point needs more than 7 decimal digits. `html.unescape` raises ValueError on a
# decimal reference of over 4300 digits (Python's int-conversion limit), and a caption's
# author controls that text, so longer ones are replaced before it runs.
_LONG_NUMERIC_REF = re.compile(r"&#[0-9]{9,};?")


@dataclass(frozen=True)
class Cue:
    start: float  # seconds
    text: str  # one line


def _clean_line(line: str) -> str:
    untagged = _LONG_NUMERIC_REF.sub(chr(0xFFFD), _TAG.sub("", line))
    return _SPACE.sub(" ", html.unescape(untagged)).strip()


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
    # Only an empty line ends a cue: YouTube starts rolling cues with a line holding a
    # single space, which must not split them.
    for block in re.split(r"\n{2,}", text):
        lines = block.split("\n")
        # Every timing line starts a cue, so a missing blank line between two cues
        # does not turn the second one's timing line into text. A header, NOTE, STYLE
        # or REGION block has none and is skipped.
        timings = [(i, m) for i, ln in enumerate(lines) if "-->" in ln and (m := _TIMING.match(ln.strip()))]
        for n, (timing_at, m) in enumerate(timings):
            start = int(m["h"] or 0) * 3600 + int(m["m"]) * 60 + int(m["s"]) + int(m["ms"]) / 1000
            end = timings[n + 1][0] if n + 1 < len(timings) else len(lines)
            body = [c for c in (_clean_line(ln) for ln in lines[timing_at + 1 : end]) if c]
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
