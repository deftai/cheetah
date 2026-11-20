import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from extractor import (
    extract_json, html_to_markdown, auto_chunk,
    ExtractionError, AIEndpointError, MalformedResponseError, SchemaValidationError,
    _load_fallback_models,
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


def test_extract_json_raises_on_non_json_http_body(mock_client):
    resp = MagicMock()
    resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    resp.text = "<html>502 Bad Gateway</html>"
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="non-JSON response"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_raises_on_unexpected_structure(mock_client):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"wrong_key": "no message"}]}
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="unexpected structure"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_raw_output_truncated_to_200_chars(mock_client):
    long_output = "x" * 500
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": long_output}}]
    }
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError) as exc_info:
            asyncio.run(extract_json("<p>test</p>", {}))
        assert len(exc_info.value.raw_output) == 200


def test_extract_json_raises_on_non_dict_json(mock_client):
    resp = MagicMock()
    resp.json.return_value = ["not", "a", "dict"]
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="non-object JSON"):
            asyncio.run(extract_json("<p>test</p>", {}))


def test_extract_json_raises_on_null_content(mock_client):
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": None}}]
    }
    resp.raise_for_status = MagicMock()
    mock_client.post.return_value = resp

    with patch("extractor._get_client", return_value=mock_client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        with pytest.raises(MalformedResponseError, match="not valid JSON") as exc_info:
            asyncio.run(extract_json("<p>test</p>", {}))
        assert exc_info.value.raw_output is None


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


# --- schema validation ---

_PERSON_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
}


def _client_returning(content):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": json.dumps(content)}}]}
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    return client


def _extract(client, schema, html="<p>x</p>"):
    with patch("extractor._get_client", return_value=client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        return asyncio.run(extract_json(html, schema))


def test_extract_json_accepts_conforming_response():
    client = _client_returning({"name": "Ada", "age": 36})
    assert _extract(client, _PERSON_SCHEMA) == {"name": "Ada", "age": 36}


def test_extract_json_rejects_wrong_type():
    client = _client_returning({"name": "Ada", "age": "thirty-six"})
    with pytest.raises(SchemaValidationError, match=r"\$\['age'\]") as exc_info:
        _extract(client, _PERSON_SCHEMA)
    assert isinstance(exc_info.value, ExtractionError)
    assert exc_info.value.errors[0][0] == "$['age']"
    assert "thirty-six" in exc_info.value.raw_output


def test_extract_json_rejects_missing_required_field():
    client = _client_returning({"age": 36})
    with pytest.raises(SchemaValidationError, match="failed 'required'") as exc_info:
        _extract(client, _PERSON_SCHEMA)
    assert "'name' is a required property" in exc_info.value.errors[0][1]


def test_extract_json_reports_all_violations():
    client = _client_returning({"age": "x"})
    with pytest.raises(SchemaValidationError, match=r"\(\+1 more\)") as exc_info:
        _extract(client, _PERSON_SCHEMA)
    assert len(exc_info.value.errors) == 2


def test_extract_json_validates_nested_paths():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "object",
                       "properties": {"price": {"type": "number"}}}}},
    }
    client = _client_returning({"items": [{"price": 1}, {"price": "free"}]})
    with pytest.raises(SchemaValidationError) as exc_info:
        _extract(client, schema)
    assert exc_info.value.errors[0][0] == "$['items'][1]['price']"


def test_extract_json_rejects_non_object_when_schema_requires_object():
    client = _client_returning(["not", "an", "object"])
    with pytest.raises(SchemaValidationError):
        _extract(client, _PERSON_SCHEMA)


def test_extract_json_truncates_raw_output_on_validation_error():
    client = _client_returning({"name": "x" * 1000, "age": "bad"})
    with pytest.raises(SchemaValidationError) as exc_info:
        _extract(client, _PERSON_SCHEMA)
    assert len(exc_info.value.raw_output) <= 200


def test_extract_json_rejects_invalid_schema_before_calling_api():
    client = _client_returning({})
    with pytest.raises(ValueError, match="invalid extraction schema"):
        _extract(client, {"type": "not-a-type"})
    client.post.assert_not_called()


# --- retry ---

@pytest.fixture(autouse=True)
def _no_backoff_sleep():
    """Keep retry tests instant; individual tests inspect sleeps via their own patch."""
    with patch("extractor.asyncio.sleep", new=AsyncMock()):
        yield


def _http_error_response(status, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status}", request=MagicMock(), response=resp,
    )
    return resp


def _ok_response(content=None):
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(content or {"ok": True})}}]
    }
    resp.raise_for_status = MagicMock()
    return resp


def _run(client):
    with patch("extractor._get_client", return_value=client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"):
        return asyncio.run(extract_json("<p>x</p>", {}))


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retries_transient_status_then_succeeds(status):
    client = AsyncMock()
    client.post.side_effect = [_http_error_response(status), _ok_response({"a": 1})]
    assert _run(client) == {"a": 1}
    assert client.post.call_count == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_does_not_retry_non_transient_status(status):
    client = AsyncMock()
    client.post.return_value = _http_error_response(status)
    with pytest.raises(AIEndpointError) as exc_info:
        _run(client)
    assert exc_info.value.status_code == status
    assert client.post.call_count == 1


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"),
])
def test_retries_connection_errors_and_timeouts(exc):
    client = AsyncMock()
    client.post.side_effect = [exc, _ok_response()]
    assert _run(client) == {"ok": True}
    assert client.post.call_count == 2


def test_gives_up_after_max_retries_and_raises_last_error():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    with patch("extractor._MAX_RETRIES", 2):
        with pytest.raises(AIEndpointError, match="HTTP 503") as exc_info:
            _run(client)
    assert client.post.call_count == 3
    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


def test_max_retries_zero_disables_retry():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    with patch("extractor._MAX_RETRIES", 0):
        with pytest.raises(AIEndpointError):
            _run(client)
    assert client.post.call_count == 1


def test_backoff_is_exponential_and_capped():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    sleep = AsyncMock()
    with patch("extractor.asyncio.sleep", new=sleep), patch("extractor._MAX_RETRIES", 6):
        with pytest.raises(AIEndpointError):
            _run(client)
    # 0.5 doubling reaches the 8s cap on the 5th retry; the 6th must not grow to 16s.
    assert [c.args[0] for c in sleep.call_args_list] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_retry_after_header_is_honored_but_capped():
    client = AsyncMock()
    client.post.side_effect = [
        _http_error_response(429, {"Retry-After": "3"}),
        _http_error_response(429, {"Retry-After": "600"}),
        _ok_response(),
    ]
    sleep = AsyncMock()
    with patch("extractor.asyncio.sleep", new=sleep):
        _run(client)
    assert [c.args[0] for c in sleep.call_args_list] == [3.0, 8.0]


def test_http_date_retry_after_falls_back_to_backoff():
    client = AsyncMock()
    client.post.side_effect = [
        _http_error_response(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _ok_response(),
    ]
    sleep = AsyncMock()
    with patch("extractor.asyncio.sleep", new=sleep):
        _run(client)
    assert [c.args[0] for c in sleep.call_args_list] == [0.5]


def test_malformed_response_is_not_retried():
    client = AsyncMock()
    resp = _ok_response()
    resp.json.return_value = {"choices": []}
    client.post.return_value = resp
    with pytest.raises(MalformedResponseError):
        _run(client)
    assert client.post.call_count == 1


# --- fallback models ---

def _models_called(client):
    return [c.kwargs["json"]["model"] for c in client.post.call_args_list]


def _run_with_models(client, primary, fallbacks, schema=None, max_retries=0):
    with patch("extractor._get_client", return_value=client), \
         patch("extractor._API_BASE", "https://api.example.com/v1"), \
         patch("extractor._MODEL", primary), \
         patch("extractor._FALLBACK_MODELS", fallbacks), \
         patch("extractor._MAX_RETRIES", max_retries):
        return asyncio.run(extract_json("<p>x</p>", schema or {}))


def test_no_fallback_models_uses_only_primary():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    with pytest.raises(AIEndpointError):
        _run_with_models(client, "primary", [])
    assert _models_called(client) == ["primary"]


def test_primary_success_never_calls_fallback():
    client = AsyncMock()
    client.post.return_value = _ok_response({"a": 1})
    assert _run_with_models(client, "primary", ["backup"]) == {"a": 1}
    assert _models_called(client) == ["primary"]


def test_falls_back_when_primary_endpoint_fails():
    client = AsyncMock()
    client.post.side_effect = [_http_error_response(503), _ok_response({"a": 1})]
    assert _run_with_models(client, "primary", ["backup"]) == {"a": 1}
    assert _models_called(client) == ["primary", "backup"]


def test_falls_back_on_model_specific_http_error():
    client = AsyncMock()
    client.post.side_effect = [_http_error_response(404), _ok_response({"a": 1})]
    assert _run_with_models(client, "primary", ["backup"]) == {"a": 1}
    assert _models_called(client) == ["primary", "backup"]


def test_falls_back_on_malformed_response():
    bad = _ok_response()
    bad.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    client = AsyncMock()
    client.post.side_effect = [bad, _ok_response({"a": 1})]
    assert _run_with_models(client, "primary", ["backup"]) == {"a": 1}
    assert _models_called(client) == ["primary", "backup"]


def test_falls_back_on_schema_mismatch():
    client = AsyncMock()
    client.post.side_effect = [_ok_response({"age": "x"}), _ok_response({"name": "Ada"})]
    assert _run_with_models(client, "primary", ["backup"], _PERSON_SCHEMA) == {"name": "Ada"}
    assert _models_called(client) == ["primary", "backup"]


@pytest.mark.parametrize("status", [401, 403])
def test_does_not_fall_back_on_auth_errors(status):
    client = AsyncMock()
    client.post.return_value = _http_error_response(status)
    with pytest.raises(AIEndpointError) as exc_info:
        _run_with_models(client, "primary", ["backup"])
    assert exc_info.value.status_code == status
    assert _models_called(client) == ["primary"]


def test_tries_fallbacks_in_order_and_raises_last_error():
    client = AsyncMock()
    client.post.side_effect = [
        _http_error_response(503), _http_error_response(502), _http_error_response(504),
    ]
    with pytest.raises(AIEndpointError, match="HTTP 504"):
        _run_with_models(client, "primary", ["b1", "b2"])
    assert _models_called(client) == ["primary", "b1", "b2"]


def test_duplicate_models_are_only_tried_once():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    with pytest.raises(AIEndpointError):
        _run_with_models(client, "primary", ["primary", "backup", "backup"])
    assert _models_called(client) == ["primary", "backup"]


def test_retries_run_per_model_before_falling_back():
    client = AsyncMock()
    client.post.side_effect = [
        _http_error_response(503), _http_error_response(503), _ok_response({"a": 1}),
    ]
    assert _run_with_models(client, "primary", ["backup"], max_retries=1) == {"a": 1}
    assert _models_called(client) == ["primary", "primary", "backup"]


def test_invalid_schema_does_not_try_any_model():
    client = AsyncMock()
    with pytest.raises(ValueError, match="invalid extraction schema"):
        _run_with_models(client, "primary", ["backup"], {"type": "nope"})
    client.post.assert_not_called()


def test_fallback_models_get_one_attempt_each_not_a_full_retry_budget():
    client = AsyncMock()
    client.post.return_value = _http_error_response(503)
    with pytest.raises(AIEndpointError):
        _run_with_models(client, "primary", ["b1", "b2"], max_retries=2)
    # primary: 1 + 2 retries; each fallback: a single attempt
    assert _models_called(client) == ["primary"] * 3 + ["b1", "b2"]


def test_fallback_list_trims_blanks_and_is_capped_to_three():
    assert _load_fallback_models(" a, b ,c,d,e") == ["a", "b", "c"]
    assert _load_fallback_models(" a/b , ,c ,") == ["a/b", "c"]
    assert _load_fallback_models("") == []


def test_fallback_list_drops_primary_and_duplicates_before_capping():
    assert _load_fallback_models("primary,a,a,b,c", "primary") == ["a", "b", "c"]


def test_401_on_a_fallback_model_stops_the_chain():
    client = AsyncMock()
    client.post.side_effect = [_http_error_response(503), _http_error_response(401)]
    with pytest.raises(AIEndpointError) as exc_info:
        _run_with_models(client, "primary", ["b1", "b2"])
    assert exc_info.value.status_code == 401
    assert _models_called(client) == ["primary", "b1"]


def test_schema_error_path_redacts_model_chosen_keys():
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    client = _client_returning({"jane.doe@example.com": "not-an-int"})
    with pytest.raises(SchemaValidationError) as exc_info:
        _extract(client, schema)
    assert "jane.doe@example.com" not in str(exc_info.value)
    assert "$[*]" in str(exc_info.value)
    # callers still get the real path
    assert exc_info.value.errors[0][0] == "$['jane.doe@example.com']"


def test_schema_error_path_keeps_schema_declared_keys():
    client = _client_returning({"name": "Ada", "age": "x"})
    with pytest.raises(SchemaValidationError, match=r"\$\['age'\]"):
        _extract(client, _PERSON_SCHEMA)


def test_fallback_warning_logs_no_extracted_data(caplog):
    secret = "jane.doe@example.com"
    client = AsyncMock()
    client.post.side_effect = [
        _ok_response({"name": "Ada", "age": secret}), _ok_response({"name": "Ada"}),
    ]
    with caplog.at_level("INFO", logger="extractor"):
        _run_with_models(client, "primary", ["backup"], _PERSON_SCHEMA)
    text = caplog.text
    assert "falling back to backup" in text and "SchemaValidationError" in text
    assert secret not in text
    assert "served by fallback model backup" in text


def test_schema_validation_error_message_omits_the_offending_value():
    client = _client_returning({"name": "Ada", "age": "jane.doe@example.com"})
    with pytest.raises(SchemaValidationError) as exc_info:
        _extract(client, _PERSON_SCHEMA)
    assert "jane.doe@example.com" not in str(exc_info.value)
    assert "failed 'type'" in str(exc_info.value)
    # detail is still available to callers that want it
    assert "jane.doe@example.com" in exc_info.value.errors[0][1]
