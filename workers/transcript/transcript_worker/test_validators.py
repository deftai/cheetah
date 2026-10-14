import pytest

from transcript_worker.validators import (
    JobRejected,
    canonical_url,
    clean_languages,
    valid_language_tag,
    valid_request_language,
    valid_video_id,
    validate_job,
)

JOB_ID = "0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20"
VID = "dQw4w9WgXcQ"


def fields(**over):
    f = {
        "job_id": JOB_ID,
        "url": canonical_url(VID),
        "format": "markdown",
        "resolved_ip": "142.250.80.46",
        "strategy": "youtube_transcript",
        "video_id": VID,
    }
    f.update(over)
    return {k: v for k, v in f.items() if v is not None}


def test_accepts_a_gateway_job():
    job = validate_job(fields())
    assert (job.job_id, job.video_id, job.language) == (JOB_ID, VID, None)
    assert job.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_language_is_optional_but_kept():
    assert validate_job(fields(language="pt-BR")).language == "pt-BR"
    assert validate_job(fields(language="ko-orig")).language == "ko-orig"


@pytest.mark.parametrize(
    "bad",
    [
        "", "all", "ALL", "a", "english", "-en", "en-", "en/../x", "en us", "en;rm", "en\n", "e\u0301n",
        "x" * 21, "en-" + "a" * 9,
    ],
)
def test_bad_languages_are_rejected(bad):
    assert not valid_request_language(bad)
    with pytest.raises(JobRejected) as e:
        validate_job(fields(language=bad))
    assert e.value.job_id == JOB_ID, "a readable job id lets the worker write a failed result"


@pytest.mark.parametrize("good", ["en", "pt-BR", "zh-Hans", "ko-orig", "en-US", "fil", "es-419"])
def test_good_languages(good):
    assert valid_request_language(good)


@pytest.mark.parametrize(
    "bad",
    [
        "dQw4w9WgXc", "dQw4w9WgXcQQ", "dQw4w9WgXc\n", "dQw4w9WgXc/", "dQw4w9WgXc.", "", "videoseries",
        "live_stream", "d\u00fcw4w9WgXcQ", None, 5,
    ],
)
def test_bad_video_ids(bad):
    assert not valid_video_id(bad)


def test_trailing_newline_is_not_tolerated():
    # `$` in a Python regex matches before a trailing newline; fullmatch must not.
    assert not valid_video_id(VID + "\n")
    assert not valid_request_language("en\n")
    assert not valid_language_tag("en\n")


@pytest.mark.parametrize(
    "over",
    [
        {"strategy": "generic"},
        {"strategy": None},
        {"video_id": "short"},
        {"video_id": None},
        {"url": "https://youtu.be/dQw4w9WgXcQ"},
        {"url": canonical_url(VID) + "&list=x"},
        {"url": canonical_url("aaaaaaaaaaa")},
        {"url": None},
        {"format": "html"},
        {"resolved_ip": "not-an-ip"},
        {"resolved_ip": 5},
        {"job_id": "not-a-uuid"},
        {"job_id": None},
        {"job_id": JOB_ID + "\n"},
    ],
)
def test_rejects_malformed_jobs(over):
    with pytest.raises(JobRejected):
        validate_job(fields(**over))


def test_unreadable_job_id_is_reported_as_none():
    with pytest.raises(JobRejected) as e:
        validate_job(fields(job_id="nope", video_id="x"))
    assert e.value.job_id is None


def test_non_string_values_are_rejected_not_coerced():
    for key in ("video_id", "url", "strategy", "language"):
        with pytest.raises(JobRejected):
            validate_job(fields(**{key: b"bytes"}))


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
        "224.0.0.1", "::1", "::", "fe80::1", "fe80::1%eth0", "fd00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
        "142.250.80.46%eth0", " 142.250.80.46", "142.250.80.46\n",
    ],
    ids=lambda s: repr(s),
)
def test_resolved_ip_must_be_a_public_address(ip):
    with pytest.raises(JobRejected):
        validate_job(fields(resolved_ip=ip))


@pytest.mark.parametrize("ip", ["142.250.80.46", "8.8.8.8", "2607:f8b0:4004:c07::65", "2001:4860:4860::8888"])
def test_public_resolved_ips_are_accepted(ip):
    assert validate_job(fields(resolved_ip=ip)).resolved_ip == ip


def test_absent_resolved_ip_and_format_are_fine():
    job = validate_job(fields(resolved_ip=None, format=None))
    assert job.resolved_ip is None


def test_language_tags_from_youtube_are_validated_and_capped():
    tags = ["en", "EN", "pt-BR", "x y", "a" * 17, "", None, "live_chat", "ko-orig", "en\n"]
    assert clean_languages(tags) == ["en", "ko-orig", "pt-BR"]
    many = [f"a{i:03d}" for i in range(300)]
    assert len(clean_languages(many)) == 100
    assert clean_languages(many) == sorted(many)[:100]
