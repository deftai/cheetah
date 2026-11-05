"""Structured extraction via serverless GPU endpoint (OpenAI-compatible API)."""

import json
import logging
import os
import re
import urllib.parse

import httpx
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

log = logging.getLogger(__name__)


_API_BASE = os.environ.get("AI_ENDPOINT_URL", "")
_API_KEY = os.environ.get("AI_API_KEY", "")
_MODEL = os.environ.get("AI_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")
try:
    _TIMEOUT = int(os.environ.get("AI_TIMEOUT", "30"))
except (ValueError, TypeError):
    _TIMEOUT = 30
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

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    url = _API_BASE.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    log.info("extraction request: url=%s content_length=%d", parsed.hostname, len(markdown))
    try:
        resp = await _get_client().post(url, headers=headers, json=payload)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise AIEndpointError(
            f"AI endpoint returned HTTP {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise AIEndpointError(str(exc)) from exc

    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedResponseError(
            f"AI endpoint returned non-JSON response: {exc}",
            raw_output=resp.text[:200],
        ) from exc

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
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(
            f"AI response is not valid JSON: {exc}",
            raw_output=content[:200],
        ) from exc


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
