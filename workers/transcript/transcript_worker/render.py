"""Markdown output for a transcript.

Same construction as the Hacker News extractor: captions are third-party text,
so they are written inside a code fence that no backtick run in the text can
close, with every line-break character normalised and control characters
blanked. Escaping Markdown shapes one by one is a losing game; inside a fence
none of them is parsed. Only our own lines (title, track description, link,
timestamps' fence) are live Markdown, and those are forced onto one line and
inline-escaped, or built from validated values.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from .captions import Cue
from .validators import canonical_url, valid_language_tag, valid_video_id

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TITLE_CHARS = 300
DEFAULT_CHUNK_SECONDS = 15.0

_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# Bidirectional controls can make text display in a different order than it reads.
_BIDI = frozenset("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_KIND_LABEL = {
    "manual": "captions written by the video's creator",
    "auto": "auto-generated captions",
    "translated": "machine-translated captions",
}


def one_line(s: str) -> str:
    """Control characters (newlines included) and U+2028/2029 become spaces."""
    return "".join(
        " " if (unicodedata.category(c) == "Cc" or c in "\u2028\u2029") else c for c in s
    )


def escape_inline(s: str) -> str:
    """Backslash-escapes what could turn third-party text in a live Markdown line
    into a link, image, HTML, code span or emphasis, plus the triggers of GFM
    autolink literals (`://`, `www.` and `@`)."""
    out: list[str] = []
    for i, c in enumerate(s):
        if c in "\\`*_[]<>@":
            esc = True
        elif c == ":":
            esc = s[i + 1 : i + 3] == "//"
        elif c == ".":
            esc = i >= 3 and s[i - 3 : i].casefold() == "www"
        else:
            esc = False
        if esc:
            out.append("\\")
        out.append(c)
    return "".join(out)


def _clean_for_fence(text: str) -> str:
    text = text.replace("\r\n", "\n")
    out: list[str] = []
    for c in text:
        if c in _LINE_BREAKS:
            out.append("\n")
        elif c in "\n\t":
            out.append(c)
        elif unicodedata.category(c) == "Cc" or c in _BIDI:
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


def fenced_block(text: str, spaces: int = 0) -> str:
    """`text` as an inert fenced block, `spaces` deep, ending in a newline."""
    pad = " " * spaces
    clean = _clean_for_fence(text)
    longest = run = 0
    for c in clean:
        if c == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * (max(longest, 2) + 1)
    lines = [pad + fence]
    for line in clean.strip("\n").split("\n"):
        lines.append(pad + line if line else "")
    lines.append(pad + fence)
    return "\n".join(lines) + "\n"


def format_timestamp(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"[{h}:{m:02d}:{sec:02d}]" if h else f"[{m:02d}:{sec:02d}]"


def chunk_cues(cues: Sequence[Cue], chunk_seconds: float = DEFAULT_CHUNK_SECONDS) -> list[tuple[float, str]]:
    """Joins consecutive cues into lines that each span about `chunk_seconds`."""
    chunks: list[tuple[float, str]] = []
    start = 0.0
    parts: list[str] = []
    for cue in cues:
        if parts and cue.start - start >= chunk_seconds:
            chunks.append((start, " ".join(parts)))
            parts = []
        if not parts:
            start = cue.start
        parts.append(cue.text)
    if parts:
        chunks.append((start, " ".join(parts)))
    return chunks


def _body(chunks: Sequence[tuple[float, str]]) -> str:
    return "\n".join(f"{format_timestamp(t)} {text}" for t, text in chunks)


def _assemble(header: str, chunks: Sequence[tuple[float, str]], total: int) -> str:
    out = header + fenced_block(_body(chunks))
    if len(chunks) < total:
        shown = format_timestamp(chunks[-1][0]) if chunks else "the start"
        out += f"\n_Transcript cut off after {shown} to stay within the size limit._\n"
    return out


def render_transcript(
    *,
    title: str,
    video_id: str,
    kind: str,
    language: str,
    cues: Sequence[Cue],
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> str:
    """The transcript as Markdown, at most `max_bytes` UTF-8 bytes.

    `video_id`, `kind` and `language` must already be validated; a bad value is a
    programming error and raises ValueError rather than reaching live Markdown.
    """
    if not valid_video_id(video_id) or kind not in _KIND_LABEL or not valid_language_tag(language):
        raise ValueError("invalid transcript header value")
    shown_title = escape_inline(one_line(title)[:MAX_TITLE_CHARS].strip()) or "YouTube video"
    header = (
        f"# {shown_title}\n\n"
        f"Transcript: {_KIND_LABEL[kind]}, language {language}\n\n"
        f"Video: <{canonical_url(video_id)}>\n\n"
    )
    chunks = chunk_cues(cues, chunk_seconds)

    def size(n: int) -> int:
        return len(_assemble(header, chunks[:n], len(chunks)).encode("utf-8"))

    if size(len(chunks)) <= max_bytes:
        return _assemble(header, chunks, len(chunks))
    # Largest prefix that fits. Size only grows with n (a longer text can only
    # lengthen the fence), so a binary search is sound.
    lo, hi = 0, len(chunks)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size(mid) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return _assemble(header, chunks[:lo], len(chunks))
