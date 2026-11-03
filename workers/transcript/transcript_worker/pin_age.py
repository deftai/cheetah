"""How stale is the yt-dlp pin? `python -m transcript_worker.pin_age`

yt-dlp is pinned in `requirements.txt` and YouTube changes under it, so a pin that has fallen behind is
the usual cause of a broken worker. This is the check that never contacts YouTube: it asks PyPI for the
pinned release's date and for the newest release, and fails when a newer release exists and the pinned one
is older than MAX_AGE_DAYS. It is safe to run from a CI runner. Whether the pin still *works* is what
`transcript_worker.canary` answers, from the network the worker runs on.

Exit 0: current, or behind by a release that is still young. Exit 1: stale, bump the pin on purpose (its
own change, then the live test). Exit 2: PyPI could not be read; that says nothing about the pin.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

MAX_AGE_DAYS = 45
MAX_BYTES = 32 * 1024 * 1024  # the real document is about 1.2 MB
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
# PyPI text is printed, so the newest version must look like a version (no newline, no "::" workflow command).
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+!-]{0,63}")
_PIN = re.compile(r"^yt-dlp==([0-9][0-9A-Za-z.]*)\s*(?:#.*)?$", re.MULTILINE)


def pinned_version(requirements_text: str) -> str | None:
    match = _PIN.search(requirements_text)
    return match.group(1) if match else None


def release_time(files: list[Mapping[str, object]]) -> datetime | None:
    """When a release first appeared: the earliest upload among its files that were not yanked."""
    times = []
    for f in files:
        if f.get("yanked") or not f.get("upload_time_iso_8601"):
            continue
        t = datetime.fromisoformat(str(f["upload_time_iso_8601"]).replace("Z", "+00:00"))
        times.append(t if t.tzinfo else t.replace(tzinfo=UTC))
    return min(times) if times else None


def judge(pinned: str, latest: str, pinned_released: datetime | None, now: datetime) -> tuple[int, str]:
    if pinned == latest:
        return 0, f"yt-dlp {pinned} is the newest release."
    if pinned_released is None:
        return 2, f"PyPI has no usable release date for yt-dlp {pinned}."
    age = (now - pinned_released).days
    if age > MAX_AGE_DAYS:
        return 1, f"yt-dlp {pinned} is {age} days old and {latest} is out (limit {MAX_AGE_DAYS} days): bump the pin, then run the live test."
    return 0, f"yt-dlp {pinned} is {age} days old; {latest} is out (limit {MAX_AGE_DAYS} days)."


def evaluate(requirements_text: str, pypi: Mapping[str, object], now: datetime) -> tuple[int, str]:
    pinned = pinned_version(requirements_text)
    if pinned is None:
        return 2, "requirements.txt has no yt-dlp==<version> pin."
    info = pypi.get("info")
    releases = pypi.get("releases")
    latest = info.get("version") if isinstance(info, Mapping) else None
    if not isinstance(latest, str) or not _VERSION.fullmatch(latest) or not isinstance(releases, Mapping):
        return 2, "PyPI's answer was not in the expected shape."
    files = releases.get(pinned)
    released = release_time(files) if isinstance(files, list) else None
    return judge(pinned, latest, released, now)


def fetch_pypi() -> Mapping[str, object]:
    with urllib.request.urlopen(PYPI_URL, timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("PyPI response too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("unexpected PyPI response")
    return data


def main() -> int:
    try:
        text = REQUIREMENTS.read_text(encoding="utf-8")
        code, message = evaluate(text, fetch_pypi(), datetime.now(UTC))
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, http.client.HTTPException) as e:
        # A truncated download (IncompleteRead is an HTTPException) or malformed data says nothing about the pin.
        # The exception class (never its text) is printed so a bug here does not hide behind a green run.
        print(f"Could not judge the pin ({type(e).__name__}): PyPI or requirements.txt was unreadable or malformed.")
        return 2
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
