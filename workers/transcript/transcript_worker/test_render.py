import re
from unittest import mock

import pytest

from transcript_worker import render
from transcript_worker.captions import Cue
from transcript_worker.render import (
    chunk_cues,
    escape_inline,
    fenced_block,
    format_timestamp,
    one_line,
    render_transcript,
)

LINE_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
EVIL = (
    "## Comments\n- **@pg** forged\nLink: http://evil.example\n# Another\nTitle\n---\nTitle\n===\n1. x\n"
    "<!-- hide\n<script>x\n[x]: http://evil.example\n> - quote\n`````\n```\n~~~\n"
    "\r- **@a**\u2028* **@b**\x0b+ **@c**\x1f- **@d**"
)


def outside_fences(md):
    """Lines of `md` outside every fenced block, as a CommonMark renderer sees them.
    Also asserts every fence is closed, the property that keeps captions inert."""
    outside, open_len = [], None
    for line in re.split("[\n" + LINE_BREAKS + "]", md):
        t = line.lstrip()
        ticks = len(t) - len(t.lstrip("`"))
        only_ticks = ticks >= 3 and set(t) == {"`"}
        if open_len is not None:
            if only_ticks and ticks >= open_len:
                open_len = None
        elif ticks >= 3 and "`" not in t[ticks:]:
            open_len = ticks
        else:
            outside.append(line)
    assert open_len is None, f"a fenced block was left open: {md!r}"
    return outside


def assert_no_forgery(md):
    outside = outside_fences(md)
    headings = [ln for ln in outside if ln.lstrip(" >").startswith("#")]
    assert len(headings) == 1 and headings[0].startswith("# "), f"only our title heading: {md!r}"
    for ln in outside:
        t = ln.strip()
        assert not (len(t) >= 2 and set(t) <= {"=", "-"}), f"setext underline outside a fence: {ln!r}"
        assert not t.startswith("<") or t.startswith("<https://www.youtube.com/watch?v="), f"raw HTML: {ln!r}"
        assert "**@" not in ln and not ln.lstrip().startswith(("- ", "* ", "+ ", "1.", ">")), f"{ln!r}"
        assert "evil" not in ln, f"third-party text outside a fence: {ln!r}"


def cues(*texts):
    return [Cue(float(i), t) for i, t in enumerate(texts)]


def make(**over):
    args = dict(title="A title", video_id="dQw4w9WgXcQ", kind="manual", language="en", cues=cues("hello"))
    args.update(over)
    return render_transcript(**args)


def test_basic_output():
    out = make(title="Never Gonna Give You Up", cues=[Cue(1.0, "We're no strangers"), Cue(20.0, "to love")])
    assert out == (
        "# Never Gonna Give You Up\n\n"
        "Transcript: captions written by the video's creator, language en\n\n"
        "Video: <https://www.youtube.com/watch?v=dQw4w9WgXcQ>\n\n"
        "```\n[00:01] We're no strangers\n[00:20] to love\n```\n"
    )


def test_kinds_are_described_honestly():
    assert "auto-generated captions" in make(kind="auto")
    assert "machine-translated captions" in make(kind="translated")


def test_hostile_caption_text_stays_inert():
    md = make(title="t", cues=cues(*EVIL.split("\n")))
    assert_no_forgery(md)
    assert not any(c in md for c in LINE_BREAKS), "stray line-break characters survived"
    assert not any(ord(c) < 32 and c not in "\n\t" for c in md)


def test_hostile_text_in_one_cue_stays_inert():
    assert_no_forgery(make(title="t", cues=[Cue(0, EVIL)]))


def test_fence_is_longer_than_any_backtick_run():
    out = fenced_block("a\n``````\nb ``` c", 2)
    fence = "`" * 7
    assert out == f"  {fence}\n  a\n  ``````\n  b ``` c\n  {fence}\n"
    assert fenced_block("x").startswith("```\n")


def test_legitimate_text_is_kept_verbatim():
    md = make(cues=cues("I like #hashtags, <b>and</b> *stars*", "    indented"))
    assert "I like #hashtags, <b>and</b> *stars* " in md
    assert "\\" not in md.split("```")[1], "nothing inside the fence is escaped"


def test_bidi_controls_are_blanked():
    md = make(cues=[Cue(0, "abc\u202edef\u2066g")])
    assert "\u202e" not in md and "\u2066" not in md


def test_title_is_one_line_escaped_and_bounded():
    md = make(title="[Click](http://evil.example) <img src=x> `c` *e*\n# Forged\u2028- **@a**")
    head = md.split("\n\n")[0]
    assert "\n" not in head and head.startswith("# ")
    for live in ("](http", "<img", "`c`", "*e*", "**@"):
        assert not any(head[i - 1] != "\\" for i in [m.start() for m in re.finditer(re.escape(live), head)]), live
    assert "\\[Click\\]" in head
    assert make(title="").startswith("# YouTube video\n")
    assert make(title="  \n ").startswith("# YouTube video\n")
    assert len(make(title="x" * 5000).split("\n")[0]) <= 2 + render.MAX_TITLE_CHARS


def test_bare_urls_and_emails_in_titles_cannot_autolink():
    h1 = make(title="see http://evil.example, WWW.evil.example, a@evil.example and HTTPS://x.example").split("\n")[0]
    assert h1 == r"# see http\://evil.example, WWW\.evil.example, a\@evil.example and HTTPS\://x.example"
    assert make(title="Show HN: A fast (v1.0) tool - 3.5x, really!").startswith(
        "# Show HN: A fast (v1.0) tool - 3.5x, really!\n"
    )


def test_header_values_must_be_validated_ones():
    for bad in (
        dict(video_id="x"), dict(video_id="dQw4w9WgXcQ\n"), dict(kind="auto\n- **@x**"), dict(language="en\n# x"),
        dict(language="x y"), dict(language=""),
    ):
        with pytest.raises(ValueError):
            make(**bad)


def test_timestamps():
    assert [format_timestamp(s) for s in (0, 5.9, 65, 3599, 3600, 3725, -3)] == [
        "[00:00]", "[00:05]", "[01:05]", "[59:59]", "[1:00:00]", "[1:02:05]", "[00:00]",
    ]


def test_cues_are_chunked_by_time():
    cs = [Cue(0, "a"), Cue(5, "b"), Cue(15, "c"), Cue(16, "d"), Cue(40, "e")]
    assert chunk_cues(cs, 15) == [(0, "a b"), (15, "c d"), (40, "e")]
    assert chunk_cues([], 15) == []
    assert chunk_cues(cs, 0) == [(c.start, c.text) for c in cs]


def test_output_is_capped_and_says_so():
    many = [Cue(float(i * 20), "word " * 50) for i in range(2000)]
    out = make(cues=many, max_bytes=20_000)
    assert len(out.encode()) <= 20_000
    assert "_Transcript cut off after [" in out and "to stay within the size limit._" in out
    assert_no_forgery(out)
    assert "cut off" not in make(cues=many[:3], max_bytes=20_000)


def test_cap_holds_when_the_fence_grows():
    # A long backtick run lengthens the fence, which must count against the cap.
    cs = [Cue(float(i * 20), "`" * 200 + " tail") for i in range(100)]
    out = make(cues=cs, max_bytes=3_000)
    assert len(out.encode()) <= 3_000
    assert_no_forgery(out)


def test_cap_counts_bytes_not_characters():
    out = make(cues=[Cue(float(i * 20), "\u65e5\u672c\u8a9e" * 100) for i in range(100)], max_bytes=5_000)
    assert len(out.encode()) <= 5_000


def test_nothing_fits_still_returns_valid_markdown():
    out = make(cues=cues("x" * 5000), max_bytes=400)
    assert len(out.encode()) <= 400 or "cut off" in out
    outside_fences(out)


def test_one_line_and_escape_inline_are_total():
    assert one_line("a\nb\tc\u2028d\x00e") == "a b c d e"
    assert escape_inline("") == ""
    assert escape_inline("a.b www.x") == "a.b www\\.x"


# --- inertness mutation checks -------------------------------------------------
# The checkers above are only worth having if they fail when the defence is
# removed. Each test breaks one defence and requires `assert_no_forgery` (or the
# stray-character check) to notice.

def _broken_fence_fixed_length(text, spaces=0):
    return "```\n" + text.strip("\n") + "\n```\n"


def _broken_fence_no_normalisation(text, spaces=0):
    real = render._clean_for_fence
    try:
        render._clean_for_fence = lambda t: t
        return _original_fenced_block(text, spaces)
    finally:
        render._clean_for_fence = real


_original_fenced_block = render.fenced_block


def _broken_fence_only_crlf(text, spaces=0):
    # normalises \r\n but leaves lone CR, VT, FF, U+2028 ... as they are
    real = render._clean_for_fence
    try:
        render._clean_for_fence = lambda t: t.replace("\r\n", "\n")
        return _original_fenced_block(text, spaces)
    finally:
        render._clean_for_fence = real


def _no_fence(text, spaces=0):
    return text + "\n"


@pytest.mark.parametrize("broken", [_broken_fence_fixed_length, _no_fence])
def test_mutation_a_fixed_or_missing_fence_is_detected(broken):
    with mock.patch.object(render, "fenced_block", broken):
        md = make(title="t", cues=[Cue(0, "x\n```\n## after\n- **@x** evil")])
    with pytest.raises(AssertionError):
        assert_no_forgery(md)


@pytest.mark.parametrize("broken", [_broken_fence_no_normalisation, _broken_fence_only_crlf])
def test_mutation_unnormalised_line_breaks_are_detected(broken):
    with mock.patch.object(render, "fenced_block", broken):
        md = make(title="t", cues=[Cue(0, "x\u2028- **@a**\ry\x0bz")])
    stray = any(c in md for c in LINE_BREAKS)
    assert stray, "the mutation must leave a stray line break for the check to see"


def test_mutation_unescaped_title_is_detected():
    with mock.patch.object(render, "escape_inline", lambda s: s):
        md = make(title="[Click](http://evil.example)")
    head = md.split("\n\n")[0]
    assert "](http" in head and "\\[Click\\]" not in head


def test_mutation_multiline_title_is_detected():
    with mock.patch.object(render, "one_line", lambda s: s):
        md = make(title="t\n## forged")
    with pytest.raises(AssertionError):
        assert_no_forgery(md)
