"""The payloads the worker writes to `job_results:<job_id>`.

The shape is the one the other workers write (and the gateway reads): a status
with `pages_total`/`pages_completed` and one result entry. A failure has status
"failed", no results and an `error` with a code from the fixed vocabulary and a
worker-written message (see `errors`).
"""

from __future__ import annotations

from typing import Any

from .cache import CacheEntry
from .errors import TranscriptError
from .validators import STRATEGY


def completed_payload(job_id: str, entry: CacheEntry) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "completed",
        "pages_total": 1,
        "pages_completed": 1,
        "results": [
            {
                "success": True,
                "data": {
                    "content": "",
                    "markdown": entry.markdown,
                    "metadata": {
                        "title": entry.title,
                        "description": "",
                        "language": entry.transcript["language"],
                        "status_code": 200,
                        "strategy": STRATEGY,
                        "transcript": {
                            "kind": entry.transcript["kind"],
                            "language": entry.transcript["language"],
                            "available_languages": list(entry.transcript["available_languages"]),  # type: ignore[call-overload]
                        },
                    },
                },
            }
        ],
    }


def failed_payload(job_id: str, error: TranscriptError) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "failed",
        "pages_total": 1,
        "pages_completed": 0,
        "results": [],
        "error": error.to_job_error(),
    }
