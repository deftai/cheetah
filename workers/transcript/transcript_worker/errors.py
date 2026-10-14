"""The worker's error vocabulary.

The gateway relays `code` and `message` to callers verbatim, so both are fixed
here: the code comes from CODES and the message is written by the worker. No
message ever interpolates yt-dlp or YouTube text. The only variable parts are
language tags that already passed `validators.valid_language_tag`.
"""

from __future__ import annotations

from collections.abc import Sequence

CODES = (
    "no_captions",
    "no_track_in_language",
    "rate_limited",
    "blocked",
    "unavailable",
    "timeout",
    "internal",
)

# Fixed reasons for `internal`, so the text stays worker-written.
_INTERNAL_MESSAGES = {
    "malformed_job": "The transcript job was malformed and was not processed.",
    "caption_too_large": "The caption file was larger than the worker accepts.",
    "caption_unparseable": "The caption file could not be read.",
    "caption_empty": "The caption track contained no text.",
    "unexpected": "The transcript could not be produced because of an internal error.",
}


class TranscriptError(Exception):
    """A job failure the worker reports as `{code, message}`."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message

    def to_job_error(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def no_captions() -> TranscriptError:
    return TranscriptError("no_captions", "This video has no captions.")


def no_track_in_language(requested: str | None, available: Sequence[str]) -> TranscriptError:
    """`requested` is None when the caller gave no language and the video's
    original language could not be worked out."""
    langs = ", ".join(available)
    if requested is None:
        msg = "The video's original language could not be determined; request a language."
    else:
        msg = f"No captions in language {requested}."
    if langs:
        msg += f" Available languages: {langs}."
    return TranscriptError("no_track_in_language", msg)


def rate_limited(*, translated: bool, original_language: str | None) -> TranscriptError:
    msg = "YouTube refused the caption request (rate limited). Try again later."
    if translated:
        msg += " Machine-translated captions are often refused"
        if original_language:
            msg += f"; request the original language ({original_language}) instead."
        else:
            msg += "; request the video's original language instead."
    return TranscriptError("rate_limited", msg)


def blocked() -> TranscriptError:
    return TranscriptError("blocked", "YouTube blocked the request from this worker's network.")


def unavailable() -> TranscriptError:
    return TranscriptError(
        "unavailable", "The video is unavailable (private, removed, age-restricted or region-locked)."
    )


def timeout() -> TranscriptError:
    return TranscriptError("timeout", "Fetching the captions timed out.")


def internal(reason: str = "unexpected") -> TranscriptError:
    return TranscriptError("internal", _INTERNAL_MESSAGES[reason])
