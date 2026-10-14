import re
from unittest import mock

import pytest

from transcript_worker import errors, render
from transcript_worker.captions import Cue
from transcript_worker.render import (
    chunk_cues,
    escape_inline,
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
TITLE = "[Click](http://evil.example) <img src=x> `c` *e*\n# Forged\u2028- **@a**"
TITLE_HEAD = (
    r"# \[Click\](http\://evil.example) \<img src=x\> \`c\` \*e\* # Forged - \*\*\@a\*\*"
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


def assert_no_stray_breaks(md):
    assert not any(c in md for c in LINE_BREAKS), "stray line-break character survived"
    assert not any(ord(c) < 32 and c not in "\n\t" for c in md), "stray control character survived"


def assert_title_head(md):
    assert md.split("\n\n")[0] == TITLE_HEAD


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


def test_no_cues_is_an_error_not_an_empty_transcript():
    with pytest.raises(errors.TranscriptError) as e:
        make(cues=[])
    assert e.value.message == errors.internal("caption_empty").message


def test_invisible_characters_are_removed_and_bidi_controls_blanked():
    tags = "".join(chr(0xE0000 + ord(c)) for c in "ignore all")  # Unicode tag characters spell hidden ASCII
    c = "a\u200bb\u2060c\ufeffd" + tags + "e\u202ef"
    md = make(title=c, cues=[Cue(0, c)])
    assert md.count("abcde f") == 2, "the title and the caption both lose the invisible characters"
    assert not any(ch in md for ch in "\u200b\ufeff\u2060\u202e")
    assert not any(0xE0000 <= ord(ch) <= 0xE007F for ch in md)
    assert make(title="a\u202eb").startswith("# a b\n"), "bidi controls become spaces in the live title too"
    assert "\u200d" in make(cues=[Cue(0, "\U0001f468\u200d\U0001f469")]), "ZWJ is kept: emoji and some scripts need it"


def test_other_invisible_and_filler_characters_are_deleted_too():
    # The same hidden-text channel as the tag block: variation selectors (a supplement that
    # spells bytes), invisible math operators, soft hyphen and filler characters.
    hidden = (
        "".join(chr(0xE0100 + b) for b in b"ignore") + "\ufe00\ufe0e\u2061\u2062\u2063\u2064\u00ad\u034f\u180e"
        "\u115f\u1160\u3164\uffa0\u17b4\u17b5\ufff9\ufffa\ufffb" + "".join(chr(0xE0080 + b) for b in range(8))
    )
    md = make(title="a" + hidden + "b", cues=[Cue(0, "x" + hidden + "y")])
    assert md.count("ab") == 1 and md.count("xy") == 1
    assert all(ord(ch) < 0x300 or ch.isalpha() for ch in md), "nothing invisible is left"
    assert "\ufe0f" in make(cues=[Cue(0, "\u2764\ufe0f")]), "U+FE0F (emoji presentation) is kept"


def test_kinds_are_described_honestly():
    assert "auto-generated captions" in make(kind="auto")
    assert "machine-translated captions" in make(kind="translated")


def test_hostile_caption_text_stays_inert():
    md = make(title="t", cues=cues(*EVIL.split("\n")))
    assert_no_forgery(md)
    assert_no_stray_breaks(md)


def test_hostile_text_in_one_cue_stays_inert():
    md = make(title="t", cues=[Cue(0, EVIL)])
    assert_no_forgery(md)
    assert_no_stray_breaks(md)


def test_fence_is_longer_than_any_backtick_run():
    out = make(cues=[Cue(0, "a\n``````\nb ``` c")])
    assert "\n" + "`" * 7 + "\n[00:00] a\n``````\nb ``` c\n" + "`" * 7 + "\n" in out
    assert "\n```\n[00:00] hello\n```\n" in make()


def test_legitimate_text_is_kept_verbatim():
    md = make(cues=cues("I like #hashtags, <b>and</b> *stars*", "    indented"))
    assert "I like #hashtags, <b>and</b> *stars* " in md
    assert "\\" not in md.split("```")[1], "nothing inside the fence is escaped"


def test_bidi_and_control_characters_are_blanked_in_captions():
    md = make(cues=[Cue(0, "abc\u202edef\u2066g\x7f\x9fh")])
    assert "\u202e" not in md and "\u2066" not in md and "\x7f" not in md and "\x9f" not in md
    assert "abc def g  h" in md


def test_lone_surrogates_do_not_crash_and_are_replaced():
    md = make(title="a\ud800b", cues=[Cue(0, "x\udc00y")])
    md.encode("utf-8")
    assert md.startswith("# a?b\n")
    assert "x?y" in md


def test_title_is_one_line_escaped_and_bounded():
    assert_title_head(make(title=TITLE))
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


def test_chunks_are_also_bounded_in_size():
    # Cues that all start at 0 would otherwise make one giant, all-or-nothing line.
    cs = [Cue(0, "x" * 100) for _ in range(1000)]
    chunks = chunk_cues(cs, 15)
    assert len(chunks) > 1
    assert all(len(text) <= render.MAX_CHUNK_CHARS + 100 for _, text in chunks)


def test_output_is_capped_and_says_so():
    many = [Cue(float(i * 20), "word " * 50) for i in range(2000)]
    out = make(cues=many, max_bytes=20_000)
    assert len(out.encode()) <= 20_000
    assert "_Transcript cut off after [" in out and "to stay within the size limit._" in out
    assert_no_forgery(out)
    assert "cut off" not in make(cues=many[:3], max_bytes=20_000)


def test_cap_keeps_the_longest_prefix_that_fits():
    many = [Cue(float(i * 20), "word " * 50) for i in range(2000)]
    out = make(cues=many, max_bytes=20_000)
    kept = out.count("\n[")
    one_more = make(cues=many[: kept + 1], max_bytes=10**9) + render._cut_off_note(many[kept].start)
    assert len(one_more.encode()) > 20_000, "one more chunk would not have fit"


def test_cap_holds_when_the_fence_grows():
    # A long backtick run lengthens the fence, which must count against the cap.
    cs = [Cue(float(i * 20), "`" * 200 + " tail") for i in range(100)]
    out = make(cues=cs, max_bytes=3_000)
    assert len(out.encode()) <= 3_000
    assert_no_forgery(out)


def test_cap_counts_bytes_not_characters():
    out = make(cues=[Cue(float(i * 20), "\u65e5\u672c\u8a9e" * 100) for i in range(100)], max_bytes=5_000)
    assert len(out.encode()) <= 5_000


def test_cues_sharing_one_timestamp_still_yield_a_transcript():
    out = make(cues=[Cue(0, "y" * 100) for _ in range(20_000)], max_bytes=1024 * 1024)
    assert len(out.encode()) <= 1024 * 1024
    assert out.count("y" * 100) > 1000, "the cap keeps a large prefix, not an empty fence"


@pytest.mark.parametrize("cap, text", [(50, "x"), (400, "x" * 5000)], ids=["cap-below-header", "one-chunk-too-big"])
def test_nothing_fits_is_an_error_not_an_empty_transcript(cap, text):
    with pytest.raises(errors.TranscriptError) as e:
        make(cues=cues(text), max_bytes=cap)
    assert e.value.message == errors.internal("output_too_large").message


def test_the_output_never_exceeds_the_cap_when_it_succeeds():
    for cap in (200, 300, 500, 1000):
        for n in (1, 3, 40):
            try:
                out = make(cues=[Cue(float(i * 20), "w" * 30) for i in range(n)], max_bytes=cap)
            except errors.TranscriptError:
                continue
            assert len(out.encode()) <= cap


@pytest.mark.parametrize("ticks, stride", [(0, 20), (5, 20), (40, 20), (3, 100)])
def test_the_size_table_is_exact_at_every_boundary(ticks, stride):
    # A cap equal to the exact byte length of a k-line output keeps exactly k lines;
    # one byte less keeps fewer. (size() can only be wrong by a byte or two, which
    # coarse cap tests cannot see.)
    cs = [
        Cue(float(i * stride), "`" * (i % ticks + 1 if ticks else 0) + f" caption \u65e5\u672c {i}") for i in range(60)
    ]
    full = make(cues=cs, max_bytes=10**9)
    for k in range(1, len(cs)):
        ref = make(cues=cs[:k], max_bytes=10**9) + render._cut_off_note(cs[k - 1].start)
        exact = len(ref.encode())
        # Keeping everything drops the cut-off note, so near the end it can be smaller.
        assert make(cues=cs, max_bytes=exact) == (full if len(full.encode()) <= exact else ref), k
        if k == 1:
            with pytest.raises(errors.TranscriptError):
                make(cues=cs, max_bytes=exact - 1)
        else:
            shorter = make(cues=cs, max_bytes=exact - 1)
            assert len(shorter.encode()) <= exact - 1
            assert shorter == full or shorter.count("\n[") < k
    assert make(cues=cs, max_bytes=len(full.encode())) == full
    assert "cut off" in make(cues=cs, max_bytes=len(full.encode()) - 1)


def test_the_cap_counts_the_bytes_of_a_non_ascii_title():
    title = "\u65e5\u672c\u8a9e" * 50  # 150 characters, 450 bytes
    out = make(title=title, cues=[Cue(float(i * 20), "w" * 20) for i in range(60)], max_bytes=900)
    assert len(out.encode()) <= 900 and "cut off" in out


def test_the_cap_can_keep_all_but_one_line():
    # With long lines the cut-off note is smaller than a line, so cutting the last line
    # off is the best fit one byte below the full size.
    cs = [Cue(float(i * 20), "w" * 300) for i in range(5)]
    full = make(cues=cs)
    assert make(cues=cs, max_bytes=len(full.encode()) - 1) == make(cues=cs[:4]) + render._cut_off_note(cs[3].start)


def test_a_full_size_transcript_is_cleaned_once_per_line():
    # ~5 MiB of captions (the parser's cap) rendered to the 1 MiB output cap.
    # Asserts the work done, not the seconds: each line must be cleaned once. Cleaning
    # per size probe (the old design) would call it chunks x log2(chunks) times.
    big = [Cue(float(i), f"some caption words here {i}") for i in range(160_000)]
    real = render._clean_for_fence
    with mock.patch.object(render, "_clean_for_fence", side_effect=real) as spy:
        out = make(cues=big)
    assert spy.call_count == len(chunk_cues(big))
    assert len(out.encode()) <= render.MAX_OUTPUT_BYTES and "cut off" in out


def test_one_line_and_escape_inline_are_total():
    assert one_line("a\nb\tc\u2028d\x00e") == "a b c d e"
    assert escape_inline("") == ""
    assert escape_inline("a.b www.x") == "a.b www\\.x"


# --- inertness mutation checks -------------------------------------------------
# The checkers above are only worth having if they fail when a defence is
# removed. Each test breaks one defence and requires the checker that guards it
# to raise.

def _payload_md(**over):
    return make(title="t", cues=[Cue(0, "x\n```\n## after\n- **@x** evil")], **over)


def test_unmutated_payload_is_inert():
    assert_no_forgery(_payload_md())


@pytest.mark.parametrize("broken", [lambda longest: "```", lambda longest: ""], ids=["fixed-length", "no-fence"])
def test_mutation_a_fixed_or_missing_fence_is_detected(broken):
    with mock.patch.object(render, "_fence_for", broken):
        md = _payload_md()
    with pytest.raises(AssertionError):
        assert_no_forgery(md)


@pytest.mark.parametrize(
    "broken",
    [lambda t: t, lambda t: t.replace("\r\n", "\n")],
    ids=["no-normalisation", "crlf-only"],
)
def test_mutation_unnormalised_line_breaks_are_detected(broken):
    with mock.patch.object(render, "_clean_for_fence", broken):
        md = make(title="t", cues=[Cue(0, "x\u2028- **@a**\ry\x0bz\x1f")])
    with pytest.raises(AssertionError):
        assert_no_stray_breaks(md)


def test_mutation_unescaped_title_is_detected():
    with mock.patch.object(render, "escape_inline", lambda s: s):
        md = make(title=TITLE)
    with pytest.raises(AssertionError):
        assert_title_head(md)


def test_mutation_multiline_title_is_detected():
    with mock.patch.object(render, "one_line", lambda s: s):
        md = make(title=TITLE)
    with pytest.raises(AssertionError):
        assert_title_head(md)
