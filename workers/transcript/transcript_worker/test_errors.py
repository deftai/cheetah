import pytest

from transcript_worker import errors


def test_vocabulary_matches_the_gateway_contract():
    assert set(errors.CODES) == {
        "no_captions", "no_track_in_language", "rate_limited", "blocked", "unavailable", "timeout", "internal",
    }


def test_unknown_code_is_a_programming_error():
    with pytest.raises(ValueError):
        errors.TranscriptError("teapot", "x")


def test_job_error_shape():
    assert errors.blocked().to_job_error() == {"code": "blocked", "message": errors.blocked().message}


def test_every_constructor_yields_a_known_code_and_a_message():
    for e in (
        errors.no_captions(), errors.no_track_in_language("en", ["fr"]), errors.no_track_in_language(None, []),
        errors.rate_limited(translated=False, original_language=None), errors.blocked(), errors.unavailable(),
        errors.timeout(), *(errors.internal(r) for r in errors._INTERNAL_MESSAGES),
    ):
        assert e.code in errors.CODES and e.message


def test_no_track_message_lists_languages():
    m = errors.no_track_in_language("en", ["fr", "ko-orig"]).message
    assert "No captions in language en." in m and "fr, ko-orig" in m
    assert "original language could not be determined" in errors.no_track_in_language(None, ["fr"]).message


def test_translated_refusal_points_at_the_original_language():
    m = errors.rate_limited(translated=True, original_language="ko-orig").message
    assert "request the original language (ko-orig)" in m
    assert "original language" not in errors.rate_limited(translated=False, original_language="ko").message
    assert "original language instead" in errors.rate_limited(translated=True, original_language=None).message


def test_errors_survive_pickling_for_a_process_pool():
    import pickle

    e = pickle.loads(pickle.dumps(errors.TranscriptError("rate_limited", "slow down", retry_after=7.5)))
    assert isinstance(e, errors.TranscriptError)
    assert (e.code, e.message, e.retry_after) == ("rate_limited", "slow down", 7.5)
    assert errors.no_captions().retry_after is None
    assert pickle.loads(pickle.dumps(errors.blocked())).to_job_error() == errors.blocked().to_job_error()


def test_retry_after_is_not_part_of_the_job_error():
    e = errors.TranscriptError("rate_limited", "slow down", retry_after=7.5)
    assert e.to_job_error() == {"code": "rate_limited", "message": "slow down"}
