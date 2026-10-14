"""Markdown output for a transcript.

Same construction as the Hacker News extractor: captions are third-party text,
so they are written inside a code fence that no backtick run in the text can
close, with every line-break character normalised and control characters
blanked. Escaping Markdown shapes one by one is a losing game; inside a fence
none of them is parsed. Only our own lines (the title, the track description
and the link) are live Markdown, and those are forced onto one line and
inline-escaped, or built from validated values.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from . import errors
from .captions import Cue
from .validators import canonical_url, valid_language_tag, valid_video_id

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TITLE_CHARS = 300
DEFAULT_CHUNK_SECONDS = 15.0
# A line also closes once it holds this much text, so cues that share one
# timestamp cannot become a single line that the size cap keeps or drops whole.
MAX_CHUNK_CHARS = 2000

_LINE_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
# Bidirectional controls can make text display in a different order than it reads.
_BIDI = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
# Characters that render as nothing, or as a blank, but that a consumer reading the text,
# such as an LLM, can read: zero-width and filler characters, the soft hyphen, invisible
# math operators, variation selectors (including the supplement, which can spell bytes) and
# the Unicode tag block (U+E0000-E0FFF, which spells ASCII). They are deleted. ZWJ and ZWNJ
# (U+200D, U+200C) and U+FE0F (emoji presentation) stay: emoji sequences and several
# scripts need them.
_INVISIBLE = (
    0xAD, 0x34F, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x180F), 0x200B, *range(0x2060, 0x2065),
    0x3164, *range(0xFE00, 0xFE0F), 0xFEFF, 0xFFA0, *range(0xFFF9, 0xFFFC), *range(0xE0000, 0xE1000),
)
_KIND_LABEL = {
    "manual": "captions written by the video's creator",
    "auto": "auto-generated captions",
    "translated": "machine-translated captions",
}
_TICKS = re.compile("`+")


def _translation(*, keep_newlines: bool) -> dict[int, str | None]:
    """Per-character rules: control characters (C0 and C1) and bidi controls become
    spaces, invisible characters are deleted, and every line-break character becomes
    a newline (inside the fence, where `\\t` and `\\n` also pass through unchanged) or a
    space (on the one-line title). A `str.translate` table runs in C; a Python loop over
    each character was the cost of rendering a 5 MiB transcript."""
    table: dict[int, str | None] = {o: " " for o in (*range(0x20), *range(0x7F, 0xA0))}
    table.update({ord(c): " " for c in _BIDI})
    table.update({o: None for o in _INVISIBLE})
    table.update({ord(c): "\n" if keep_newlines else " " for c in _LINE_BREAKS})
    if keep_newlines:
        del table[ord("\t")], table[ord("\n")]
    return table


_FENCE_TABLE = _translation(keep_newlines=True)
_TITLE_TABLE = _translation(keep_newlines=False)


def _utf8_safe(s: str) -> str:
    """Lone surrogates (from a JSON `\\ud800`) cannot be UTF-8 encoded; they
    become `?` instead of crashing the job."""
    return s.encode("utf-8", "replace").decode("utf-8")


def one_line(s: str) -> str:
    """Control characters (newlines included), U+2028/2029 and bidi controls become
    spaces; invisible characters are deleted."""
    return s.translate(_TITLE_TABLE)


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
    return _utf8_safe(text).replace("\r\n", "\n").translate(_FENCE_TABLE)


def _fence_for(longest_backtick_run: int) -> str:
    """A fence longer than any backtick run in the text, so it cannot close it."""
    return "`" * (max(longest_backtick_run, 2) + 1)


def _longest_run(s: str) -> int:
    return max((len(m) for m in _TICKS.findall(s)), default=0)


def format_timestamp(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"[{h}:{m:02d}:{sec:02d}]" if h else f"[{m:02d}:{sec:02d}]"


def chunk_cues(cues: Sequence[Cue], chunk_seconds: float = DEFAULT_CHUNK_SECONDS) -> list[tuple[float, str]]:
    """Joins consecutive cues into lines that each span about `chunk_seconds`
    and hold about `MAX_CHUNK_CHARS` characters at most."""
    chunks: list[tuple[float, str]] = []
    start = 0.0
    parts: list[str] = []
    chars = 0
    for cue in cues:
        if parts and (cue.start - start >= chunk_seconds or chars >= MAX_CHUNK_CHARS):
            chunks.append((start, " ".join(parts)))
            parts, chars = [], 0
        if not parts:
            start = cue.start
        parts.append(cue.text)
        chars += len(cue.text) + 1
    if parts:
        chunks.append((start, " ".join(parts)))
    return chunks


def _cut_off_note(last_kept_start: float) -> str:
    return f"\n_Transcript cut off after {format_timestamp(last_kept_start)} to stay within the size limit._\n"


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

    Too long a transcript is cut off at a line boundary, with a note. If not even
    the header and one line fit, it raises TranscriptError(internal) rather than
    return an empty transcript. `video_id`, `kind` and `language` must already be
    validated; a bad value is a programming error and raises ValueError rather
    than reaching live Markdown.
    """
    if not valid_video_id(video_id) or kind not in _KIND_LABEL or not valid_language_tag(language):
        raise ValueError("invalid transcript header value")
    shown_title = escape_inline(one_line(_utf8_safe(title)[:MAX_TITLE_CHARS]).strip()) or "YouTube video"
    header = (
        f"# {shown_title}\n\n"
        f"Transcript: {_KIND_LABEL[kind]}, language {language}\n\n"
        f"Video: <{canonical_url(video_id)}>\n\n"
    )
    chunks = chunk_cues(cues, chunk_seconds)
    if not chunks:
        raise errors.internal("caption_empty")
    lines = [_clean_for_fence(f"{format_timestamp(t)} {text}").strip("\n") for t, text in chunks]
    total = len(lines)

    # One pass gives the byte size and longest backtick run of every prefix, so
    # each size question below is O(1): no re-cleaning, no re-encoding.
    body_bytes: list[int] = []
    longest: list[int] = []
    cum = run = 0
    for i, line in enumerate(lines):
        cum += len(line.encode("utf-8")) + (1 if i else 0)
        run = max(run, _longest_run(line))
        body_bytes.append(cum)
        longest.append(run)
    header_bytes = len(header.encode("utf-8"))

    def size(n: int) -> int:
        """Exact byte size of the output that keeps the first `n` (at least one) lines:
        the header, the opening and closing fence (each with its newline), the body and
        its trailing newline, and the cut-off note unless every line is kept."""
        out = header_bytes + 2 * (len(_fence_for(longest[n - 1])) + 1) + body_bytes[n - 1] + 1
        if n < total:
            out += len(_cut_off_note(chunks[n - 1][0]).encode("utf-8"))
        return out

    if size(total) <= max_bytes:
        keep = total
    else:
        # The largest prefix that fits. Size only grows with n (more text, a
        # fence that can only lengthen), so a binary search is sound.
        lo, hi = 0, total - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if size(mid) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        keep = lo
        if keep == 0:
            raise errors.internal("output_too_large")

    fence = _fence_for(longest[keep - 1])
    out = header + fence + "\n" + "\n".join(lines[:keep]) + "\n" + fence + "\n"
    if keep < total:
        out += _cut_off_note(chunks[keep - 1][0])
    return out
