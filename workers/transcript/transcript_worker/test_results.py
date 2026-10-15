import json

from transcript_worker import errors
from transcript_worker.cache import CacheEntry
from transcript_worker.results import completed_payload, failed_payload

JOB_ID = "0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20"
ENTRY = CacheEntry(
    markdown="# A video\n\n```\n[00:01] hi\n```\n",
    title="A video",
    transcript={"kind": "auto", "language": "ko-orig", "available_languages": ["ko-orig"]},
)


def test_completed_payload_has_the_shape_the_gateway_reads():
    assert completed_payload(JOB_ID, ENTRY) == {
        "job_id": JOB_ID,
        "status": "completed",
        "pages_total": 1,
        "pages_completed": 1,
        "results": [
            {
                "success": True,
                "data": {
                    "content": "",
                    "markdown": ENTRY.markdown,
                    "metadata": {
                        "title": "A video",
                        "description": "",
                        "language": "ko-orig",
                        "status_code": 200,
                        "strategy": "youtube_transcript",
                        "transcript": {"kind": "auto", "language": "ko-orig", "available_languages": ["ko-orig"]},
                    },
                },
            }
        ],
    }


def test_failed_payload_carries_the_error_and_no_results():
    p = failed_payload(JOB_ID, errors.blocked())
    assert p == {
        "job_id": JOB_ID,
        "status": "failed",
        "pages_total": 1,
        "pages_completed": 0,
        "results": [],
        "error": {"code": "blocked", "message": errors.blocked().message},
    }


def test_payloads_are_json_with_no_surprises():
    for p in (completed_payload(JOB_ID, ENTRY), failed_payload(JOB_ID, errors.timeout())):
        assert json.loads(json.dumps(p)) == p


def test_the_payload_does_not_alias_the_cache_entry():
    p = completed_payload(JOB_ID, ENTRY)
    p["results"][0]["data"]["metadata"]["transcript"]["available_languages"].append("zz")
    assert ENTRY.transcript["available_languages"] == ["ko-orig"]


def test_the_error_message_is_the_workers_own_text():
    for err in (errors.no_captions(), errors.unavailable(), errors.timeout(), errors.internal("unexpected")):
        assert failed_payload(JOB_ID, err)["error"] == {"code": err.code, "message": err.message}
