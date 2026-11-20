"""Structured extraction via serverless GPU endpoint (OpenAI-compatible API)."""

import asyncio
import json
import logging
import os
import re
import urllib.parse

import httpx
import jsonschema
from markdownify import markdownify


class ExtractionError(Exception):
    """Base for all extraction-related failures."""


class AIEndpointError(ExtractionError):
    """Network or HTTP error communicating with the AI endpoint."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class MalformedResponseError(ExtractionError):
    """AI endpoint returned a response that isn't usable JSON."""

    def __init__(self, message: str, raw_output: str | None = None):
        super().__init__(message)
        self.raw_output = raw_output


class SchemaValidationError(ExtractionError):
    """AI response is valid JSON but does not match the caller's schema.

    ``errors`` lists each violation as ``(json_path, message)``; ``raw_output``
    is a truncated copy of the received JSON.
    """

    def __init__(
        self,
        message: str,
        errors: list[tuple[str, str]] | None = None,
        raw_output: str | None = None,
    ):
        super().__init__(message)
        self.errors = errors or []
        self.raw_output = raw_output


log = logging.getLogger(__name__)


_API_BASE = os.environ.get("AI_ENDPOINT_URL", "")
_API_KEY = os.environ.get("AI_API_KEY", "")
_MODEL = os.environ.get("AI_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")
_MAX_FALLBACK_MODELS = 3


def _load_fallback_models(raw: str, primary: str = "") -> list[str]:
    """Parse a comma-separated model list into the fallbacks tried after ``primary`` fails.

    Blanks, duplicates and the primary itself are dropped *before* the cap, so the cap
    (which bounds worst-case latency) counts only models that will actually be tried.
    """
    names = (m.strip() for m in raw.split(","))
    unique = dict.fromkeys(m for m in names if m and m != primary)
    return list(unique)[:_MAX_FALLBACK_MODELS]


_FALLBACK_MODELS = _load_fallback_models(os.environ.get("AI_FALLBACK_MODELS", ""), _MODEL)
try:
    _TIMEOUT = int(os.environ.get("AI_TIMEOUT", "30"))
except (ValueError, TypeError):
    _TIMEOUT = 30
try:
    _MAX_RETRIES = max(0, int(os.environ.get("AI_MAX_RETRIES", "2")))
except (ValueError, TypeError):
    _MAX_RETRIES = 2
_RETRY_BASE_DELAY = 0.5
_RETRY_MAX_DELAY = 8.0
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client

_STRIP_TAGS_RE = re.compile(
    r"<\s*(script|style|noscript)[^>]*>.*?</\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a structured data extractor. Given page content and a JSON schema, "
    "extract the requested fields from the content. Respond with valid JSON only — "
    "no markdown fences, no commentary."
)


async def extract_json(html: str, schema: dict) -> dict:
    """Call a serverless GPU endpoint to extract structured data from HTML.

    Converts HTML to markdown first (cheaper tokens), then sends
    markdown + schema to an OpenAI-compatible chat completions endpoint
    with JSON response format.
    """
    if not _API_BASE:
        raise RuntimeError("AI_ENDPOINT_URL not configured")

    # Fail before spending a model call if the caller's schema is itself invalid.
    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(f"invalid extraction schema: {exc.message}") from exc

    parsed = urllib.parse.urlparse(_API_BASE)
    if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
        raise RuntimeError("AI_ENDPOINT_URL must use HTTPS")

    truncated = html[:200_000] if len(html) > 200_000 else html
    markdown = html_to_markdown(truncated)

    if len(markdown) > 48_000:
        markdown = markdown[:48_000]

    user_prompt = (
        f"Extract data according to this JSON schema:\n"
        f"```json\n{json.dumps(schema)}\n```\n\n"
        f"From this content:\n\n{markdown}"
    )

    headers = {}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    url = _API_BASE.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    models = list(dict.fromkeys([_MODEL, *_FALLBACK_MODELS]))
    log.info("extraction request: url=%s content_length=%d", parsed.hostname, len(markdown))
    for index, model in enumerate(models):
        # Only the primary gets the full retry budget; fallbacks get one attempt each so the
        # worst case is (retries + 1) + len(fallbacks) calls, not models x (retries + 1).
        max_retries = _MAX_RETRIES if index == 0 else 0
        try:
            result = await _extract_with_model(
                model, url, headers, messages, schema, validator_cls, max_retries,
            )
        except ExtractionError as exc:
            if index == len(models) - 1 or not _should_try_next_model(exc):
                raise
            # Log only non-content fields: exception text can hold extracted page data (PII).
            log.warning(
                "model %s failed (%s, status=%s); falling back to %s",
                model, type(exc).__name__, getattr(exc, "status_code", None), models[index + 1],
            )
        else:
            if index > 0:
                log.info("extraction served by fallback model %s (%d of %d)",
                         model, index + 1, len(models))
            return result
    raise AssertionError("unreachable")  # pragma: no cover


def _should_try_next_model(exc: ExtractionError) -> bool:
    """Another model can help with a bad model, reply or outage, but not with bad credentials."""
    return not (isinstance(exc, AIEndpointError) and exc.status_code in (401, 403))


async def _extract_with_model(
    model: str,
    url: str,
    headers: dict[str, str],
    messages: list[dict[str, str]],
    schema: dict,
    validator_cls: type,
    max_retries: int,
) -> dict:
    """One model's attempt: call (with retries), parse, and validate the reply."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    resp = await _post_with_retry(url, headers, payload, max_retries)

    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedResponseError(
            f"AI endpoint returned non-JSON response: {exc}",
            raw_output=resp.text[:200],
        ) from exc

    if not isinstance(body, dict):
        raise MalformedResponseError(
            "AI endpoint returned non-object JSON",
            raw_output=str(body)[:200],
        )

    choices = body.get("choices")
    if not choices:
        raise MalformedResponseError("AI endpoint returned no choices")

    try:
        content = choices[0]["message"]["content"]
    except (KeyError, TypeError, IndexError) as exc:
        raise MalformedResponseError(
            f"AI response has unexpected structure: {exc}",
        ) from exc

    try:
        result = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedResponseError(
            f"AI response is not valid JSON: {exc}",
            raw_output=str(content)[:200] if content is not None else None,
        ) from exc

    _validate_against_schema(result, schema, validator_cls)
    return result


def _validate_against_schema(result, schema: dict, validator_cls) -> None:
    """Raise SchemaValidationError if ``result`` does not conform to ``schema``."""
    violations = sorted(
        validator_cls(schema).iter_errors(result),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if not violations:
        return
    errors = [(_format_path(e.absolute_path), e.message[:200]) for e in violations]
    # The message names only the path and the failed keyword (type, required, ...): jsonschema's
    # own text quotes the offending value, which is extracted page data and must not reach logs.
    # Full detail stays on ``errors`` / ``raw_output`` for callers that need it.
    first = violations[0]
    first_path = _format_path(first.absolute_path, redact_keys=_has_open_keys(first))
    extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    failed = first.validator
    raise SchemaValidationError(
        f"AI response does not match schema at {first_path}: failed '{failed}'{extra}",
        errors=errors,
        raw_output=json.dumps(result)[:200],
    )


# Schema keywords under which object keys come from the model's output, not from the schema.
_OPEN_KEY_KEYWORDS = frozenset(
    {"additionalProperties", "patternProperties", "unevaluatedProperties", "propertyNames"}
)


def _has_open_keys(error: jsonschema.ValidationError) -> bool:
    return not _OPEN_KEY_KEYWORDS.isdisjoint(error.absolute_schema_path)


def _format_path(parts, redact_keys: bool = False) -> str:
    """Render a JSON path like ``$['items'][1]['price']``.

    With ``redact_keys``, object keys are shown as ``[*]``: they came from the model's output
    (e.g. an email used as a key under ``additionalProperties``) and may be personal data.
    """
    return "$" + "".join(
        ("[*]" if redact_keys else f"[{p!r}]") if isinstance(p, str) else f"[{p}]"
        for p in parts
    )


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Exponential backoff (0.5s, 1s, 2s, ...), or the server's Retry-After if larger."""
    delay = _RETRY_BASE_DELAY * (2 ** attempt)
    if response is not None:
        try:
            delay = max(delay, float(response.headers.get("Retry-After", 0)))
        except (TypeError, ValueError):
            pass  # HTTP-date form of Retry-After: fall back to backoff
    return min(delay, _RETRY_MAX_DELAY)


async def _post_with_retry(
    url: str, headers: dict, payload: dict, max_retries: int,
) -> httpx.Response:
    """POST to the AI endpoint, retrying transient failures.

    Retries connection errors and timeouts, and HTTP 429/502/503/504, up to
    ``max_retries`` times. Other HTTP errors (400/401/403/500...) fail at once.
    """
    for attempt in range(max_retries + 1):
        response = None
        try:
            response = await _get_client().post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = AIEndpointError(f"AI endpoint returned HTTP {status}", status_code=status)
            retryable = status in _RETRYABLE_STATUS
            cause, response = exc, exc.response
        except httpx.TransportError as exc:
            error = AIEndpointError(str(exc))
            retryable = True
            cause, response = exc, None
        except httpx.HTTPError as exc:
            raise AIEndpointError(str(exc)) from exc

        if not retryable or attempt >= max_retries:
            raise error from cause
        delay = _retry_delay(attempt, response)
        log.warning(
            "AI endpoint call failed (%s); retry %d/%d in %.1fs",
            error, attempt + 1, max_retries, delay,
        )
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def html_to_markdown(html: str) -> str:
    """Convert HTML to clean markdown."""
    cleaned = _STRIP_TAGS_RE.sub("", html)
    md = markdownify(cleaned, heading_style="ATX", strip=["img"])
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def auto_chunk(markdown: str, max_tokens: int = 512) -> list[dict]:
    """Split markdown into chunks by headings, then by token budget.

    Returns a list of dicts with content, heading, and estimated token count.
    """
    chars_per_token = 4
    max_chars = max_tokens * chars_per_token

    sections = _split_by_headings(markdown)
    chunks = []

    for heading, body in sections:
        text = f"{heading}\n\n{body}".strip() if heading else body.strip()
        if not text:
            continue

        if len(text) <= max_chars:
            chunks.append({
                "content": text,
                "heading": heading,
                "tokens": len(text) // chars_per_token,
            })
        else:
            for part in _split_text(text, max_chars):
                chunks.append({
                    "content": part,
                    "heading": heading,
                    "tokens": len(part) // chars_per_token,
                })

    return chunks


def _split_by_headings(markdown: str) -> list[tuple[str | None, str]]:
    pattern = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)
    parts = pattern.split(markdown)

    sections: list[tuple[str | None, str]] = []
    if parts[0].strip():
        sections.append((None, parts[0]))

    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((heading, body))

    return sections


def _split_text(text: str, max_chars: int) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks
