import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from extractor import (
    extract_json, html_to_markdown, auto_chunk,
    ExtractionError, AIEndpointError, MalformedResponseError,
)


# --- html_to_markdown ---

def test_html_to_markdown_basic():
    html = "<h1>Title</h1><p>Hello <strong>world</strong></p>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "**world**" in md


def test_html_to_markdown_strips_scripts():
    html = "<p>Keep</p><script>alert(1)</script><style>.x{}</style>"
    md = html_to_markdown(html)
    assert "Keep" in md
    assert "alert" not in md
    assert ".x" not in md


def test_html_to_markdown_strips_noscript():
    html = "<p>Visible</p><noscript>Enable JS</noscript>"
    md = html_to_markdown(html)
    assert "Visible" in md
    assert "Enable JS" not in md


def test_html_to_markdown_collapses_blanks():
    html = "<p>A</p><br><br><br><br><p>B</p>"
    md = html_to_markdown(html)
    assert "\n\n\n" not in md


def test_html_to_markdown_empty():
    assert html_to_markdown("") == ""


# --- auto_chunk ---

def test_auto_chunk_single_short():
    md = "# Intro\n\nShort paragraph."
    chunks = auto_chunk(md, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0]["heading"] == "# Intro"
    assert chunks[0]["tokens"] > 0


def test_auto_chunk_multiple_headings():
    md = "# One\n\nText one.\n\n# Two\n\nText two."
    chunks = auto_chunk(md, max_tokens=512)
    assert len(chunks) == 2
    assert chunks[0]["heading"] == "# One"
    assert chunks[1]["heading"] == "# Two"


def test_auto_chunk_splits_long_section():
    long_text = "word " * 1000
    md = f"# Big\n\n{long_text}"
    chunks = auto_chunk(md, max_tokens=128)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk["tokens"] <= 128 or len(chunk["content"].split("\n\n")) == 1


def test_auto_chunk_no_heading():
    md = "Just a plain paragraph with no heading."
    chunks = auto_chunk(md, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0]["heading"] is None


def test_auto_chunk_empty():
    assert auto_chunk("") == []


# --- extract_json ---

@pytest.fixture
def mock_completion():
    extracted = {"name": "Acme Corp", "founded": 2020}
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(extracted)}}]
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp, extracted


@pytest.fixture
def mock_client():
    client = AsyncMock()
    return client


def test_extract_json_calls_api(mock_completion, mock_client):
    mock_resp, expected = mock_completion
    mock_client.post.return_value = mock_resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = asyncio.run(extract_json("<p>Acme Corp founded 2020</p>", schema))

    assert result == expected
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "chat/completions" in call_args[0][0]
    payload = call_args.kwargs.get("json") or call_args[1].get("json")
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0


def test_extract_json_raises_on_http_error(mock_client):
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server Error", request=MagicMock(), response=resp,
    )
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(AIEndpointError, match="HTTP 500") as exc_info:
            asyncio.run(extract_json("<p>test</p>", {}))
        assert exc_info.value.status_code == 500
        assert isinstance(exc_info.value, ExtractionError)


def test_extract_json_raises_on_connection_error(mock_client):
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(AIEndpointError, match="Connection refused"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_raises_on_no_choices(mock_client):
    resp = MagicMock()
    resp.json.return_value = {"choices": []}
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="no choices"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_raises_on_invalid_json_response(mock_client):
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": "not json {{"}}]
    }
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="not valid JSON") as exc_info:
            asyncio.run(extract_json("<p>test</p>", {}))
        assert exc_info.value.raw_output == "not json {{"


def test_extract_json_raises_without_endpoint():
    with patch("extractor._API_BASE", ""):
        with pytest.raises(RuntimeError, match="AI_ENDPOINT_URL not configured"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_rejects_http_endpoint():
    with patch("extractor._API_BASE", "http://api.example.com/v1"):
        with pytest.raises(RuntimeError, match="must use HTTPS"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_allows_localhost_http(mock_completion, mock_client):
    mock_resp, expected = mock_completion
    mock_client.post.return_value = mock_resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "http://localhost:8000/v1"):
        result = asyncio.run(extract_json("<p>Acme Corp founded 2020</p>", {}))

    assert result == expected
