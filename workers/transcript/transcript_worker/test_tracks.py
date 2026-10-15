import pytest

from transcript_worker import errors
from transcript_worker.captions import Cue
from transcript_worker.render import render_transcript
from transcript_worker.tracks import Selection, TrackListing, select_track, tracks_from_info

NATIVE = [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=x&lang=en"}]
TRANS = [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=x&lang=en&tlang=en"}]


def info(manual=(), native=(), translated=(), **extra):
    d = {
        "subtitles": {k: NATIVE for k in manual},
        "automatic_captions": {**{k: NATIVE for k in native}, **{k: TRANS for k in translated}},
    }
    d.update(extra)
    return d


def pick(i, lang=None):
    return select_track(tracks_from_info(i), lang).track


def test_listing_marks_translated_tracks_by_tlang():
    tr = {t.language: t for t in tracks_from_info(info(native=["ko-orig"], translated=["en"])).tracks}
    assert tr["ko-orig"].kind == "auto" and tr["en"].kind == "translated"


def test_manual_tracks_are_never_translated_even_if_the_url_says_so():
    listing = tracks_from_info({"subtitles": {"en": TRANS}})
    assert listing.tracks[0].kind == "manual"


def test_manual_beats_native_beats_translated():
    i = info(manual=["en"], native=["en", "en-orig"], translated=["fr"])
    assert pick(i, "en").kind == "manual"
    assert pick(info(native=["en", "en-orig"], translated=["fr"]), "en").language == "en-orig"
    assert pick(info(native=["en"], translated=["fr"]), "en").language == "en"
    t = pick(info(native=["ko-orig"], translated=["en"]), "en")
    assert (t.language, t.kind) == ("en", "translated")


def test_native_track_beats_translated_one_of_the_same_language():
    # The spike's failing video: en is machine-translated, ko-orig is native.
    i = info(native=["ko-orig"], translated=["en"])
    assert pick(i, "ko").language == "ko-orig"
    assert pick(i, "ko-orig").language == "ko-orig"


def test_orig_request_accepts_only_a_native_orig_track():
    with pytest.raises(errors.TranscriptError) as e:
        pick(info(manual=["ko"], native=["en"]), "ko-orig")
    assert e.value.code == "no_track_in_language"


def test_no_language_means_the_original_spoken_language():
    i = info(manual=["en", "fr"], native=["ko-orig"], translated=["de"])
    assert pick(i).language == "ko-orig"
    assert pick(info(manual=["en", "fr"], language="fr")).language == "fr"
    assert tracks_from_info(i).spoken_language == "ko"


def test_a_regional_spoken_language_still_finds_the_original_speech_track():
    # info["language"] may be "en-US" while the track is the bare "en-orig".
    i = info(native=["en-orig"], translated=["fr"], language="en-US")
    assert pick(i).language == "en-orig"
    assert pick(info(manual=["en"], language="en-GB")).language == "en"
    # An explicit request is not widened: asking for en-US does not match en-orig.
    with pytest.raises(errors.TranscriptError):
        pick(info(native=["en-orig"]), "en-US")


def test_a_regional_spoken_language_falls_back_to_a_translated_track_by_primary_subtag():
    assert pick(info(translated=["en", "fr"], language="en-US")).kind == "translated"
    assert pick(info(translated=["en", "fr"], language="en-US")).language == "en"


def test_a_bare_orig_key_does_not_become_the_spoken_language():
    # "-orig" with no language in front of it is not an original-speech track.
    auto = {"-orig": [{"url": "u"}], "en": [{"url": "u"}], "fr": [{"url": "u"}]}
    listing = tracks_from_info({"automatic_captions": auto})
    assert listing.spoken_language is None
    with pytest.raises(errors.TranscriptError) as e:
        select_track(listing, None)
    assert e.value.code == "no_track_in_language" and "could not be determined" in e.value.message


def test_only_tags_a_caller_could_request_become_tracks_or_a_spoken_language():
    # These tags reach a yt-dlp --sub-langs argument later: "all" means every track, and a
    # leading "-" is an exclusion. Neither is a tag a caller could request.
    for bad in ("all", "ALL", "-en", "--proxy", "en-", "x"):
        listing = tracks_from_info({"subtitles": {bad: [{"url": "u"}], "en": [{"url": "u"}]}, "language": bad})
        assert [t.language for t in listing.tracks] == ["en"], bad
        assert listing.spoken_language is None, bad


def test_every_selectable_language_can_be_listed_and_rendered():
    # A long tag a caller can request (three subtags, 19 characters) must work end to end:
    # become a track, appear in available_languages, and render.
    tag = "ca-Latn-ES-valencia"
    sel = select_track(tracks_from_info(info(manual=[tag])), tag)
    assert sel.track.language == tag and tag in sel.transcript_meta()["available_languages"]
    out = render_transcript(title="t", video_id="dQw4w9WgXcQ", kind=sel.track.kind, language=tag, cues=[Cue(0, "x")])
    assert f"language {tag}" in out


def test_declared_language_wins_over_orig_track():
    i = info(manual=["en"], native=["ko-orig"], language="en")
    assert tracks_from_info(i).spoken_language == "en"
    assert pick(i).language == "en"


def test_no_language_and_unknown_original_with_several_languages_is_an_error():
    with pytest.raises(errors.TranscriptError) as e:
        pick(info(manual=["en", "fr"]))
    assert e.value.code == "no_track_in_language"
    assert "could not be determined" in e.value.message and "en, fr" in e.value.message


def test_no_language_with_a_single_language_picks_it():
    assert pick(info(manual=["en-GB"])).language == "en-GB"
    assert pick(info(manual=["en"], native=["en-US"])).language == "en"


def test_bare_language_accepts_a_regional_variant_but_a_regional_request_does_not_widen():
    assert pick(info(manual=["en-GB", "en-US"]), "en").language == "en-GB"
    assert pick(info(native=["en-US"]), "en").language == "en-US"
    with pytest.raises(errors.TranscriptError):
        pick(info(manual=["en"]), "en-GB")
    assert pick(info(manual=["pt-BR"]), "PT-br").language == "pt-BR", "matching ignores case, output keeps YouTube's"


def test_no_captions_and_wrong_language_are_different_errors():
    with pytest.raises(errors.TranscriptError) as e:
        select_track(tracks_from_info({"subtitles": {}, "automatic_captions": {}}), "en")
    assert e.value.code == "no_captions"
    with pytest.raises(errors.TranscriptError) as e:
        pick(info(manual=["fr", "de"], translated=["ja"]), "en")
    assert e.value.code == "no_track_in_language"
    assert "Available languages: de, fr." in e.value.message, "translated languages are not advertised"


def test_only_translated_tracks_are_advertised_when_nothing_else_exists():
    listing = tracks_from_info(info(translated=["de", "ja"]))
    assert listing.available_languages() == ["de", "ja"]


def test_third_party_garbage_is_dropped_not_raised():
    bad = {
        "subtitles": {"live_chat": NATIVE, "en\n": NATIVE, "x" * 40: NATIVE, "fr": [], "de": "no", "es": NATIVE},
        "automatic_captions": ["not", "a", "dict"],
        "language": {"x": 1},
    }
    listing = tracks_from_info(bad)
    assert [t.language for t in listing.tracks] == ["es"]
    assert listing.spoken_language is None
    assert tracks_from_info({}).tracks == ()
    assert tracks_from_info({"subtitles": None, "automatic_captions": 5}).tracks == ()
    assert tracks_from_info({"subtitles": {"en": [5, "x", None]}}).tracks[0].language == "en"


def test_invalid_declared_language_is_ignored():
    assert tracks_from_info(info(manual=["en"], language="not valid!")).spoken_language is None


def test_transcript_meta_has_only_validated_strings_and_is_capped():
    langs = [f"en-x{i:03d}" for i in range(150)]
    sel = select_track(tracks_from_info(info(manual=langs)), "en-x001")
    meta = sel.transcript_meta()
    assert meta["kind"] == "manual" and meta["language"] == "en-x001"
    assert len(meta["available_languages"]) == 100
    assert isinstance(sel, Selection)


def test_original_language_hint_for_refused_translations():
    assert tracks_from_info(info(native=["ko-orig"], translated=["en"])).original_language_hint() == "ko-orig"
    assert tracks_from_info(info(manual=["en"], language="en")).original_language_hint() == "en"
    assert TrackListing(tracks=()).original_language_hint() is None
