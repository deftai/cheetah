import http.client
from datetime import UTC, datetime, timedelta

import pytest

from transcript_worker import pin_age

NOW = datetime(2026, 11, 3, tzinfo=UTC)
REQ = "# comment\nredis==8.1.0\nyt-dlp==2026.8.19\n"


def pypi(latest, releases):
    return {"info": {"version": latest}, "releases": {v: [{"upload_time_iso_8601": t}] for v, t in releases.items()}}


def test_the_pin_is_read_from_requirements():
    assert pin_age.pinned_version(REQ) == "2026.8.19"
    assert pin_age.pinned_version("yt-dlp==2026.8.19  # why\n") == "2026.8.19"
    assert pin_age.pinned_version("yt-dlp>=2026.1.1\n") is None
    assert pin_age.pinned_version("not-yt-dlp==1.0\n") is None


def test_the_newest_release_is_current():
    code, msg = pin_age.evaluate(REQ, pypi("2026.8.19", {"2026.8.19": "2026-08-19T10:00:00Z"}), NOW)
    assert code == 0 and "newest" in msg


def test_a_young_pin_that_is_one_release_behind_passes():
    releases = {"2026.8.19": "2026-10-01T00:00:00Z", "2026.10.20": "2026-10-20T00:00:00Z"}
    code, _ = pin_age.evaluate(REQ, pypi("2026.10.20", releases), NOW)
    assert code == 0


def test_an_old_pin_with_a_newer_release_is_stale():
    releases = {"2026.8.19": "2026-08-19T10:00:00Z", "2026.10.20": "2026-10-20T00:00:00Z"}
    code, msg = pin_age.evaluate(REQ, pypi("2026.10.20", releases), NOW)
    assert code == 1 and "2026.10.20" in msg and "live test" in msg


def test_the_age_limit_is_in_days_and_exclusive():
    at_limit = NOW - timedelta(days=pin_age.MAX_AGE_DAYS)
    assert pin_age.judge("2026.8.19", "2026.10.20", at_limit, NOW)[0] == 0
    assert pin_age.judge("2026.8.19", "2026.10.20", at_limit - timedelta(days=1), NOW)[0] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"info": {}, "releases": {}},
        {"info": {"version": "2026.10.20"}, "releases": {}},
        {"info": {"version": "2026.10.20"}, "releases": {"2026.8.19": [{"yanked": True, "upload_time_iso_8601": "2026-08-19T00:00:00Z"}]}},
        {"info": {"version": 5}, "releases": {}},
    ],
)
def test_a_pypi_answer_that_cannot_be_judged_is_inconclusive_not_stale(bad):
    assert pin_age.evaluate(REQ, bad, NOW)[0] == 2


def test_a_missing_pin_is_inconclusive():
    assert pin_age.evaluate("redis==8.1.0\n", pypi("1", {}), NOW)[0] == 2


def test_the_earliest_unyanked_upload_is_the_release_date():
    files = [
        {"upload_time_iso_8601": "2026-08-20T00:00:00Z"},
        {"upload_time_iso_8601": "2026-08-19T00:00:00Z"},
        {"upload_time_iso_8601": "2026-08-01T00:00:00Z", "yanked": True},
    ]
    assert pin_age.release_time(files) == datetime(2026, 8, 19, tzinfo=UTC)


def test_the_checked_in_requirements_have_a_pin_this_check_can_read():
    assert pin_age.pinned_version(pin_age.REQUIREMENTS.read_text(encoding="utf-8")) is not None


def test_a_timestamp_without_a_timezone_is_read_as_utc():
    files = [{"upload_time_iso_8601": "2026-08-19T00:00:00"}]
    assert pin_age.release_time(files) == datetime(2026, 8, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    "error",
    [http.client.IncompleteRead(b"secret-1", 5), ValueError("secret-2"), OSError("secret-3"), RecursionError("secret-4")],
)
def test_main_maps_a_failed_or_truncated_download_to_exit_2(monkeypatch, capsys, error):
    def boom():
        raise error

    monkeypatch.setattr(pin_age, "fetch_pypi", boom)
    assert pin_age.main() == 2
    out = capsys.readouterr().out
    assert type(error).__name__ in out and "secret" not in out


def test_main_maps_malformed_release_data_to_exit_2_not_a_traceback(monkeypatch, capsys):
    bad = {"info": {"version": "2026.10.20"}, "releases": {"2026.8.19": [{"upload_time_iso_8601": "not a date"}]}}
    monkeypatch.setattr(pin_age, "fetch_pypi", lambda: bad)
    assert pin_age.main() == 2
    out = capsys.readouterr().out
    assert "ValueError" in out and "not a date" not in out


def test_a_newest_version_that_is_not_a_version_is_never_printed():
    releases = {"2026.8.19": "2026-10-01T00:00:00Z", "x": "2026-10-20T00:00:00Z"}
    code, msg = pin_age.evaluate(REQ, pypi("2026.10.20\n::warning::x", releases), NOW)
    assert code == 2 and "::" not in msg
