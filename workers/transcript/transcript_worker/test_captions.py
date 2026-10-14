import time

import pytest

from transcript_worker import errors
from transcript_worker.captions import Cue, parse_vtt

MANUAL = b"""WEBVTT
Kind: captions
Language: en

NOTE a comment
that spans lines

STYLE
::cue { color: red }

1
00:00:01.000 --> 00:00:03.500 align:start position:0%
Hello &amp; welcome

00:01:02.250 --> 00:01:04.000
<v Speaker>Second</v> <i>cue</i>
two lines

01:00:00.000 --> 01:00:01.000
An hour in
"""

# YouTube's rolling auto captions: each cue repeats the previous last line.
ROLLING = b"""WEBVTT

00:00:00.000 --> 00:00:02.000
we are going

00:00:02.000 --> 00:00:02.010
we are going

00:00:02.010 --> 00:00:04.000
we are going
to the <00:00:03.000><c>shop</c>

00:00:04.000 --> 00:00:04.010
to the shop

00:00:04.010 --> 00:00:06.000
to the shop
and back
"""


def texts(cues):
    return [(c.start, c.text) for c in cues]


def test_parses_manual_captions():
    assert texts(parse_vtt(MANUAL)) == [
        (1.0, "Hello & welcome"),
        (62.25, "Second cue two lines"),
        (3600.0, "An hour in"),
    ]


def test_rolling_dedupe_for_auto_captions():
    assert texts(parse_vtt(ROLLING, rolling_dedupe=True)) == [
        (0.0, "we are going"),
        (2.01, "to the shop"),
        (4.01, "and back"),
    ]


def test_without_rolling_dedupe_repeats_are_kept():
    assert len(parse_vtt(ROLLING)) == 5, "manual captions may legitimately repeat a line"
    again = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNo.\n\n00:00:03.000 --> 00:00:04.000\nNo.\n"
    assert [c.text for c in parse_vtt(again)] == ["No.", "No."]


def test_entities_are_decoded_after_tags_are_stripped():
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n&lt;b&gt;not bold&lt;/b&gt; &amp;amp; a&nbsp;b\n"
    assert parse_vtt(vtt)[0].text == "<b>not bold</b> &amp; a b"


def test_crlf_cr_and_bom():
    vtt = "\ufeffWEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nhi\r\r00:00:03,000 --> 00:00:04,000\rthere\r"
    assert texts(parse_vtt(vtt.encode())) == [(1.0, "hi"), (3.0, "there")]


def test_unicode_line_separators_do_not_split_cues():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nfake\u2028\n\u2029more\n".encode()
    cues = parse_vtt(vtt)
    assert len(cues) == 1 and "fake" in cues[0].text


def test_invalid_utf8_is_replaced_not_fatal():
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nbad \xff\xfe bytes\n"
    assert "bad" in parse_vtt(vtt)[0].text


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"<html><body>429 Too Many Requests</body></html>", "caption_unparseable"),
        (b"", "caption_unparseable"),
        (b"\n\nWEBVTT\n", "caption_unparseable"),
        (b"WEBVTT\n\n", "caption_empty"),
        (b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n", "caption_empty"),
        (b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c></c>\n", "caption_empty"),
        (b"WEBVTT\n\nnot a timing --> line\ntext\n", "caption_empty"),
        (b"WEBVTT\n\n99999:00:01.000 --> 00:00:02.000\ntext\n", "caption_empty"),
    ],
)
def test_unusable_files_fail_with_internal(data, reason):
    with pytest.raises(errors.TranscriptError) as e:
        parse_vtt(data)
    assert (e.value.code, e.value.message) == ("internal", errors.internal(reason).message)


def test_huge_numeric_character_references_do_not_crash():
    # html.unescape raises ValueError on a decimal reference of more than 4300 digits
    # (Python's int-conversion limit); a caption's author controls that text.
    for ref in ("&#" + "1" * 4301, "&#" + "1" * 4301 + ";", "&#x" + "1" * 5000 + ";", "&#" + "9" * 50 + ";"):
        cues = parse_vtt(f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nbefore {ref} after\n".encode())
        assert cues[0].text.startswith("before ") and cues[0].text.endswith(" after")
    assert parse_vtt(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n&#65;&#x42;&#0000067;\n")[0].text == "ABC"


def test_size_is_checked_before_parsing():
    big = b"WEBVTT\n\n" + b"00:00:01.000 --> 00:00:02.000\nx\n\n" * 10
    with pytest.raises(errors.TranscriptError) as e:
        parse_vtt(big, max_bytes=len(big) - 1)
    assert e.value.message == errors.internal("caption_too_large").message
    assert parse_vtt(big, max_bytes=len(big))


@pytest.mark.parametrize("filler", ["<", "<a", "&", "\n \t" * 1, " "])
def test_pathological_files_parse_in_linear_time(filler):
    # A tag pattern that rescans to the end of the line for every `<` takes
    # minutes on a 5 MiB file of them (it took 15 s on 1 MB here).
    line = "00:00:01.000 --> 00:00:02.000\n" + filler * 250_000 + "x\n\n"
    start = time.perf_counter()
    try:
        parse_vtt(("WEBVTT\n\n" + line * 4).encode())
    except errors.TranscriptError:
        pass  # whitespace-only text has no cue; only the running time matters
    assert time.perf_counter() - start < 3.0


def test_rolling_dedupe_is_linear_in_lines_per_cue():
    # `line not in previous_lines` over a list is quadratic: two cues of 20k
    # distinct lines took 8 s, and a 5 MiB file would take about half an hour.
    n = 20_000
    cue = lambda t, tag: f"00:00:0{t}.000 --> 00:00:0{t + 1}.000\n" + "\n".join(f"{tag}{i:06d}" for i in range(n))  # noqa: E731
    vtt = ("WEBVTT\n\n" + cue(1, "a") + "\n\n" + cue(2, "b") + "\n").encode()
    start = time.perf_counter()
    cues = parse_vtt(vtt, rolling_dedupe=True)
    assert time.perf_counter() - start < 1.0
    assert len(cues) == 2


def test_cue_is_a_plain_value():
    assert Cue(1.0, "x") == Cue(1.0, "x")
