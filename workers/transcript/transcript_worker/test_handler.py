import json

import pytest

from transcript_worker import errors
from transcript_worker.cache import CacheEntry
from transcript_worker.handler import JobHandler
from transcript_worker.results import completed_payload, failed_payload

JOB_ID = "0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20"
VID = "dQw4w9WgXcQ"
ENTRY = CacheEntry(
    markdown="# A video\n\n```\n[00:01] hi\n```\n",
    title="A video",
    transcript={"kind": "auto", "language": "ko-orig", "available_languages": ["ko-orig"]},
)


def fields(**over):
    f = {
        "job_id": JOB_ID,
        "url": f"https://www.youtube.com/watch?v={VID}",
        "format": "markdown",
        "resolved_ip": "142.250.80.46",
        "strategy": "youtube_transcript",
        "video_id": VID,
        "payload": "{}",
    }
    f.update(over)
    return {k: v for k, v in f.items() if v is not None}


class Service:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.jobs = result, raises, []

    def run(self, job):
        self.jobs.append(job)
        if self.raises:
            raise self.raises
        return self.result


def test_a_good_job_runs_through_the_service():
    svc = Service(result=ENTRY)
    out = JobHandler(svc)(fields())
    assert out.job_id == JOB_ID and out.payload == completed_payload(JOB_ID, ENTRY)
    assert [j.video_id for j in svc.jobs] == [VID]


def test_a_transcript_error_becomes_a_failed_result_with_its_code():
    out = JobHandler(Service(raises=errors.no_captions()))(fields())
    assert out.payload == failed_payload(JOB_ID, errors.no_captions())


def test_retry_after_does_not_leak_into_the_result():
    err = errors.TranscriptError("rate_limited", "slow", retry_after=12.0)
    out = JobHandler(Service(raises=err))(fields())
    assert out.payload["error"] == {"code": "rate_limited", "message": "slow"}


def test_an_unexpected_exception_is_internal_with_a_fixed_message():
    out = JobHandler(Service(raises=RuntimeError("password=hunter2 at /srv/x")))(fields())
    assert out.payload["status"] == "failed"
    assert out.payload["error"] == errors.internal("unexpected").to_job_error()
    assert "hunter2" not in json.dumps(out.payload)


def test_a_malformed_job_with_a_readable_id_still_gets_a_failed_result():
    svc = Service(result=ENTRY)
    out = JobHandler(svc)(fields(url="https://evil.example/", language="-en"))
    assert out.job_id == JOB_ID
    assert out.payload == failed_payload(JOB_ID, errors.internal("malformed_job"))
    assert svc.jobs == []


@pytest.mark.parametrize("bad_id", [None, "", "not-a-uuid", "../../etc/passwd", "a" * 100])
def test_a_job_without_a_usable_id_has_nowhere_to_write(bad_id):
    out = JobHandler(Service(result=ENTRY))(fields(job_id=bad_id))
    assert out.job_id is None and out.payload is None


def test_the_outcome_has_a_log_line_without_third_party_text():
    out = JobHandler(Service(result=ENTRY))(fields())
    assert VID in out.summary and "completed" in out.summary
    bad = JobHandler(Service(raises=errors.blocked()))(fields())
    assert "blocked" in bad.summary
