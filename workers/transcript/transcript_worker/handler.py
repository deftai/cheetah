"""From a stream entry's fields to the result payload. Runs in a worker thread."""

from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import errors
from .cache import CacheEntry
from .results import completed_payload, failed_payload
from .validators import Job, JobRejected, validate_job

log = logging.getLogger(__name__)


class Service(Protocol):
    def run(self, job: Job) -> CacheEntry: ...


@dataclass(frozen=True)
class JobOutcome:
    job_id: str | None  # None when the entry had no usable job id
    payload: dict[str, Any] | None  # None when there is nowhere to write a result
    summary: str  # one log line: ids and codes only, no third-party text


class JobHandler:
    """Validates a job, runs it, and always returns an outcome: every failure becomes a failed result with a
    code from the fixed vocabulary, so a job is never left to time out on the caller's side."""

    def __init__(self, service: Service) -> None:
        self._service = service

    def __call__(self, fields: Mapping[str, str]) -> JobOutcome:
        try:
            job = validate_job(fields)
        except JobRejected as rejected:
            if rejected.job_id is None:
                return JobOutcome(None, None, "dropped a job entry without a usable job id")
            err = errors.internal("malformed_job")
            return JobOutcome(rejected.job_id, failed_payload(rejected.job_id, err), f"job={rejected.job_id} failed code={err.code} (malformed)")
        try:
            entry = self._service.run(job)
        except errors.TranscriptError as e:
            return JobOutcome(job.job_id, failed_payload(job.job_id, e), f"job={job.job_id} video={job.video_id} failed code={e.code}")
        except Exception as e:
            # The class and where it was raised, never the message: it may carry anything (signed URLs, caption
            # text, a Redis URL), and the log outlives the cache TTL.
            frames = traceback.extract_tb(e.__traceback__)
            where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else "?"
            log.error("job %s: unexpected %s at %s", job.job_id, type(e).__name__, where)
            err = errors.internal("unexpected")
            return JobOutcome(job.job_id, failed_payload(job.job_id, err), f"job={job.job_id} video={job.video_id} failed code={err.code}")
        kind = entry.transcript["kind"]
        return JobOutcome(
            job.job_id, completed_payload(job.job_id, entry), f"job={job.job_id} video={job.video_id} completed track={kind}"
        )
