package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gofiber/fiber/v2"
)

func TestMCPDiscoveryReturnsTools(t *testing.T) {
	app := testApp()

	req := httptest.NewRequest(http.MethodGet, "/.well-known/mcp.json", nil)
	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	body, _ := io.ReadAll(resp.Body)
	var result struct {
		Name     string    `json:"name"`
		Version  string    `json:"version"`
		Endpoint string    `json:"endpoint"`
		Tools    []mcpTool `json:"tools"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if result.Name != "cheetah" {
		t.Fatalf("expected name=cheetah, got %s", result.Name)
	}
	if result.Endpoint != "/v1/mcp" {
		t.Fatalf("expected endpoint=/v1/mcp, got %s", result.Endpoint)
	}
	if len(result.Tools) != 5 {
		t.Fatalf("expected 5 tools, got %d", len(result.Tools))
	}

	names := map[string]bool{}
	for _, tool := range result.Tools {
		names[tool.Name] = true
	}
	for _, want := range []string{"scrape", "crawl", "extract", "get_repository_content", "get_thread"} {
		if !names[want] {
			t.Fatalf("missing tool: %s", want)
		}
	}
}

func TestMCPDiscoveryNoAuth(t *testing.T) {
	app := testApp()

	req := httptest.NewRequest(http.MethodGet, "/.well-known/mcp.json", nil)
	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("discovery should not require auth, got %d", resp.StatusCode)
	}
}

func mcpRequest(method string, id interface{}, params interface{}) []byte {
	req := map[string]interface{}{
		"jsonrpc": "2.0",
		"method":  method,
	}
	if id != nil {
		req["id"] = id
	}
	if params != nil {
		req["params"] = params
	}
	b, _ := json.Marshal(req)
	return b
}

func postMCP(t *testing.T, app *fiber.App, body []byte) (*http.Response, map[string]interface{}) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/mcp", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", "test-key-123")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}

	respBody, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	if len(respBody) > 0 {
		if err := json.Unmarshal(respBody, &result); err != nil {
			t.Fatalf("invalid JSON response: %v\nbody: %s", err, string(respBody))
		}
	}
	return resp, result
}

func TestMCPInitialize(t *testing.T) {
	app := testApp()
	body := mcpRequest("initialize", 1, map[string]interface{}{
		"protocolVersion": "2024-11-05",
		"capabilities":    map[string]interface{}{},
		"clientInfo":      map[string]interface{}{"name": "test", "version": "1.0"},
	})

	resp, result := postMCP(t, app, body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	r, ok := result["result"].(map[string]interface{})
	if !ok {
		t.Fatal("missing result")
	}
	if r["protocolVersion"] != "2024-11-05" {
		t.Fatalf("expected protocolVersion=2024-11-05, got %v", r["protocolVersion"])
	}
	si, ok := r["serverInfo"].(map[string]interface{})
	if !ok {
		t.Fatal("missing serverInfo")
	}
	if si["name"] != "cheetah" {
		t.Fatalf("expected serverInfo.name=cheetah, got %v", si["name"])
	}
}

func TestMCPToolsList(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/list", 2, nil)

	resp, result := postMCP(t, app, body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	r, ok := result["result"].(map[string]interface{})
	if !ok {
		t.Fatal("missing result")
	}
	tools, ok := r["tools"].([]interface{})
	if !ok {
		t.Fatal("missing tools array")
	}
	if len(tools) != 5 {
		t.Fatalf("expected 5 tools, got %d", len(tools))
	}
}

func TestMCPToolCallScrapeValidatesURL(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 3, map[string]interface{}{
		"name": "scrape",
		"arguments": map[string]interface{}{
			"url":    "https://example.com",
			"format": "markdown",
		},
	})

	resp, result := postMCP(t, app, body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	r, ok := result["result"].(map[string]interface{})
	if !ok {
		t.Fatal("missing result")
	}
	content, ok := r["content"].([]interface{})
	if !ok || len(content) == 0 {
		t.Fatal("missing content in tool result")
	}
}

func TestMCPToolCallCrawlValidatesURL(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 4, map[string]interface{}{
		"name": "crawl",
		"arguments": map[string]interface{}{
			"url":       "https://example.com",
			"max_pages": 5,
		},
	})

	resp, result := postMCP(t, app, body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}

	r, ok := result["result"].(map[string]interface{})
	if !ok {
		t.Fatal("missing result")
	}
	content, ok := r["content"].([]interface{})
	if !ok || len(content) == 0 {
		t.Fatal("missing content in tool result")
	}
}

func TestMCPToolCallExtractNoEndpoint(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 5, map[string]interface{}{
		"name": "extract",
		"arguments": map[string]interface{}{
			"url":            "https://example.com",
			"extract_schema": map[string]interface{}{"type": "object"},
		},
	})

	_, result := postMCP(t, app, body)
	r := result["result"].(map[string]interface{})
	if r["isError"] != true {
		t.Fatal("expected isError=true when AI endpoint not configured")
	}
	content := r["content"].([]interface{})
	text := content[0].(map[string]interface{})["text"].(string)
	if text != "extraction service not configured" {
		t.Fatalf("unexpected error: %s", text)
	}
}

func TestMCPToolCallExtractBlockedOnFreePlan(t *testing.T) {
	cfg := Config{
		RedisURL:      "localhost:6379",
		APIKeys:       []string{"test-key-123"},
		Port:          "3000",
		AIEndpointURL: "https://ai.example.com/v1",
	}
	queue := NewQueueClient(cfg.RedisURL)
	app := NewApp(cfg, queue)

	body := mcpRequest("tools/call", 6, map[string]interface{}{
		"name": "extract",
		"arguments": map[string]interface{}{
			"url":            "https://example.com",
			"extract_schema": map[string]interface{}{"type": "object"},
		},
	})

	_, result := postMCP(t, app, body)
	r := result["result"].(map[string]interface{})
	if r["isError"] != true {
		t.Fatal("expected isError=true on free plan")
	}
}

func TestMCPToolCallUnknownTool(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 7, map[string]interface{}{
		"name":      "unknown_tool",
		"arguments": map[string]interface{}{},
	})

	_, result := postMCP(t, app, body)
	if result["error"] == nil {
		t.Fatal("expected JSON-RPC error for unknown tool")
	}
	e := result["error"].(map[string]interface{})
	if e["code"].(float64) != -32602 {
		t.Fatalf("expected error code -32602, got %v", e["code"])
	}
}

func TestMCPMethodNotFound(t *testing.T) {
	app := testApp()
	body := mcpRequest("nonexistent/method", 8, nil)

	_, result := postMCP(t, app, body)
	if result["error"] == nil {
		t.Fatal("expected JSON-RPC error for unknown method")
	}
	e := result["error"].(map[string]interface{})
	if e["code"].(float64) != -32601 {
		t.Fatalf("expected error code -32601, got %v", e["code"])
	}
}

func TestMCPParseError(t *testing.T) {
	app := testApp()
	req := httptest.NewRequest(http.MethodPost, "/v1/mcp", bytes.NewBufferString("not json"))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", "test-key-123")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}

	respBody, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	json.Unmarshal(respBody, &result)

	if result["error"] == nil {
		t.Fatal("expected JSON-RPC error for parse error")
	}
	e := result["error"].(map[string]interface{})
	if e["code"].(float64) != -32700 {
		t.Fatalf("expected error code -32700, got %v", e["code"])
	}
}

func TestMCPNotification(t *testing.T) {
	app := testApp()
	body := mcpRequest("notifications/initialized", nil, nil)

	req := httptest.NewRequest(http.MethodPost, "/v1/mcp", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", "test-key-123")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("expected 202 for notification, got %d", resp.StatusCode)
	}
}

func TestMCPScrapeRejectsSSRF(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 9, map[string]interface{}{
		"name": "scrape",
		"arguments": map[string]interface{}{
			"url": "http://169.254.169.254/latest/meta-data/",
		},
	})

	_, result := postMCP(t, app, body)
	r := result["result"].(map[string]interface{})
	if r["isError"] != true {
		t.Fatal("expected isError=true for SSRF attempt")
	}
}

func TestMCPScrapeInvalidFormat(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/call", 10, map[string]interface{}{
		"name": "scrape",
		"arguments": map[string]interface{}{
			"url":    "https://example.com",
			"format": "pdf",
		},
	})

	_, result := postMCP(t, app, body)
	r := result["result"].(map[string]interface{})
	if r["isError"] != true {
		t.Fatal("expected isError=true for invalid format")
	}
}

func TestMCPRequiresAuth(t *testing.T) {
	app := testApp()
	body := mcpRequest("tools/list", 1, nil)

	req := httptest.NewRequest(http.MethodPost, "/v1/mcp", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401 without API key, got %d", resp.StatusCode)
	}
}

func TestMCPToolCallExtractRequiresSchema(t *testing.T) {
	cfg := Config{
		RedisURL:      "localhost:6379",
		APIKeys:       []string{"test-key-123"},
		Port:          "3000",
		AIEndpointURL: "https://ai.example.com/v1",
	}
	queue := NewQueueClient(cfg.RedisURL)
	proPlan := Plan{Name: "pro", CanExtract: true, MaxRatePerMinute: 100}
	app := NewApp(cfg, queue, WithPlanLoader(&stubPlanLoader{plan: proPlan}))

	body := mcpRequest("tools/call", 11, map[string]interface{}{
		"name": "extract",
		"arguments": map[string]interface{}{
			"url": "https://example.com",
		},
	})

	_, result := postMCP(t, app, body)
	r := result["result"].(map[string]interface{})
	if r["isError"] != true {
		t.Fatal("expected isError=true when extract_schema missing")
	}
}

func toolResultText(t *testing.T, result map[string]interface{}) (string, bool) {
	t.Helper()
	r, ok := result["result"].(map[string]interface{})
	if !ok {
		t.Fatalf("missing result: %v", result)
	}
	content, ok := r["content"].([]interface{})
	if !ok || len(content) == 0 {
		t.Fatalf("missing content in tool result: %v", r)
	}
	text, _ := content[0].(map[string]interface{})["text"].(string)
	isError, _ := r["isError"].(bool)
	return text, isError
}

func callPlatformTool(t *testing.T, tool string, args map[string]interface{}) (string, bool) {
	t.Helper()
	body := mcpRequest("tools/call", 30, map[string]interface{}{"name": tool, "arguments": args})
	_, result := postMCP(t, testApp(), body)
	return toolResultText(t, result)
}

func TestPlatformToolError(t *testing.T) {
	const github = "https://github.com/rockbox37/cheetah"
	const hn = "https://news.ycombinator.com/item?id=8863"
	tests := []struct {
		tool, url string
		want      string // substring of the error, or "" for accepted
	}{
		{"get_repository_content", github, ""},
		{"get_repository_content", github + "/blob/main/README.md", ""},
		{"get_repository_content", github + "/issues/5", ""},
		{"get_thread", hn, ""},
		{"get_repository_content", hn, `belongs to "get_thread"`},
		{"get_thread", github, `belongs to "get_repository_content"`},
		{"get_repository_content", "https://example.com/a/b", `use "scrape"`},
		{"get_thread", "https://example.com/item?id=1", `use "scrape"`},
		// Look-alikes and non-content pages of the right site are not the platform.
		{"get_repository_content", "https://github.com/settings/profile", `use "scrape"`},
		{"get_repository_content", "https://github.com/rockbox37", `use "scrape"`},
		{"get_repository_content", "https://github.com.evil.example/o/r", `use "scrape"`},
		{"get_thread", "https://news.ycombinator.com/", `use "scrape"`},
		{"get_thread", "https://news.ycombinator.com/item?id=abc", `use "scrape"`},
		{"get_thread", "not a url", `use "scrape"`},
		// Host tricks: the platform is the URL's real host, not text elsewhere in it.
		{"get_repository_content", "https://github.com@evil.example/o/r", `use "scrape"`},
		{"get_repository_content", "https://evil.example/?x=github.com/o/r", `use "scrape"`},
		{"get_thread", "https://evil.example/item?id=1&r=news.ycombinator.com", `use "scrape"`},
		{"get_repository_content", "https://GitHub.COM/o/r", ""},
		{"get_repository_content", "https://www.github.com/o/r.git", ""},
		{"get_repository_content", "https://github.com:8443/o/r", ""},
		{"get_repository_content", "https://github.com/o/r/pull/12", ""},
		// Right platform, but a page the extractor does not handle: refused, not silently scraped.
		{"get_repository_content", "http://github.com/o/r", "not supported"},
		{"get_repository_content", "https://github.com/o/r/tree/main/docs", "not supported"},
		{"get_repository_content", "https://github.com/o/r/issues", "not supported"},
		{"get_repository_content", "https://github.com/o/r/issues/5/extra", "not supported"},
		{"get_repository_content", "https://github.com/o/r/issues/abc", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/src/", "not supported"},
		{"get_repository_content", "https://github.com/o/r/actions/runs/1", "not supported"},
		{"get_thread", "http://news.ycombinator.com/item?id=1", "not supported"},
		{"get_thread", "https://news.ycombinator.com/item?id=12345678901", "not supported"},
		// Gateway and worker must agree on the raw URL, not the decoded one.
		{"get_repository_content", "https://github.com/o%2Fr", "not supported"},
		{"get_repository_content", "https://github.com/%41/r", "not supported"},
		{"get_repository_content", "https://github.com/o/r%2Fx", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main%2Ffoo", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/a/b%2Fc", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a%5Cb", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a%00b", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/%FF.md", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/..", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/.", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/%2e%2E/x", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/../x/y", "not supported"},
		{"get_repository_content", `https://github.com/o/r/blob/main/a\b`, "not supported"},
		{"get_repository_content", "https://github.com:99999/o/r", "not supported"},
		{"get_thread", "https://news.ycombinator.com/%69tem?id=1", "not supported"},
		{"get_thread", "https://news.ycombinator.com/item?id=%zz&id=5", "not supported"},
		{"get_thread", "https://news.ycombinator.com:99999/item?id=1", "not supported"},
		{"get_thread", "https://news.ycombinator.com/item?id=", `use "scrape"`}, // the router already refuses an empty id
		// A path with characters Go re-escapes must not change how `%2F` is read.
		{"get_repository_content", "https://github.com/o/r/blob/main/a%2Fb|c", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a%2Fb/é", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/ma%2Fin/x|", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a|b.md", ""},
		{"get_repository_content", "https://github.com/o/r/blob/main/é.md", ""},
		{"get_repository_content", "https://github.com/o/r/blob/main/dir/é/", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/x?y=%2F#a/b", ""},
		{"get_repository_content", "HTTPS://github.com/o/r", ""},
		// Whitespace is stripped by the worker's parser but not Go's, so it is refused.
		{"get_repository_content", "https://github.com/o/r/blob/main/dir/ ", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/ ", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a.go ", "not supported"},
		{"get_repository_content", "https://github.com/o/r/blob/main/a\u00a0b.go", "not supported"},
		{"get_thread", "https://news.ycombinator.com/item?id=1 ", `use "scrape"`}, // the router already refuses a non-numeric id
		{"get_repository_content", " https://github.com/o/r", `use "scrape"`},
		// Encoded characters that the worker does decode stay supported.
		{"get_repository_content", "https://github.com/o/r/blob/main/My%20Doc%C3%A9.md", ""},
		{"get_repository_content", "https://github.com/o/r/blob/feat%2Bx/src/lib.rs", ""},
		{"get_thread", "https://news.ycombinator.com/item?id=8863&p=2", ""},
		{"get_thread", "https://news.ycombinator.com/item?id=8863#c_8864", ""},
	}
	for _, tt := range tests {
		t.Run(tt.tool+" "+tt.url, func(t *testing.T) {
			got := platformToolError(tt.tool, tt.url, ClassifyURL(tt.url))
			if tt.want == "" {
				if got != "" {
					t.Fatalf("expected %s to be accepted, got %q", tt.url, got)
				}
				return
			}
			if !strings.Contains(got, tt.want) {
				t.Fatalf("error %q should contain %q", got, tt.want)
			}
			if !strings.HasPrefix(got, tt.tool+" only accepts ") {
				t.Fatalf("error %q should name the tool and what it accepts", got)
			}
			// (A URL that is itself a prefix of the accepted-shape example is not an echo.)
			if !strings.Contains(platformTools[tt.tool].accepts, tt.url) && strings.Contains(got, tt.url) {
				t.Fatalf("error must not echo the submitted URL: %q", got)
			}
		})
	}
}

func TestMCPPlatformToolsRejectWrongPlatform(t *testing.T) {
	text, isErr := callPlatformTool(t, "get_repository_content", map[string]interface{}{"url": "https://news.ycombinator.com/item?id=8863"})
	if !isErr || !strings.Contains(text, `belongs to "get_thread"`) {
		t.Fatalf("HN URL should be rejected by get_repository_content, naming get_thread: isError=%v %q", isErr, text)
	}
	text, isErr = callPlatformTool(t, "get_thread", map[string]interface{}{"url": "https://github.com/rockbox37/cheetah/issues/5"})
	if !isErr || !strings.Contains(text, `belongs to "get_repository_content"`) {
		t.Fatalf("GitHub URL should be rejected by get_thread, naming get_repository_content: isError=%v %q", isErr, text)
	}
	for _, tool := range []string{"get_repository_content", "get_thread"} {
		text, isErr = callPlatformTool(t, tool, map[string]interface{}{"url": "https://example.com/page?token=secret"})
		if !isErr || !strings.Contains(text, `use "scrape"`) {
			t.Fatalf("%s: other URL should be rejected pointing at scrape: isError=%v %q", tool, isErr, text)
		}
		if strings.Contains(text, "secret") || strings.Contains(text, "example.com") {
			t.Fatalf("%s: error echoed the submitted URL: %q", tool, text)
		}
	}
}

func TestMCPPlatformToolsRequireURL(t *testing.T) {
	for _, tool := range []string{"get_repository_content", "get_thread"} {
		for _, args := range []map[string]interface{}{{}, {"url": ""}} {
			text, isErr := callPlatformTool(t, tool, args)
			if !isErr || text != "url is required" {
				t.Fatalf("%s %v: expected \"url is required\", got isError=%v %q", tool, args, isErr, text)
			}
		}
	}
}

func TestMCPPlatformToolsAcceptRightPlatform(t *testing.T) {
	// Past the platform check the call proceeds like scrape: it needs DNS and a
	// queue, so here it may fail at either, but never with the platform error.
	for tool, url := range map[string]string{
		"get_repository_content": "https://github.com/rockbox37/cheetah",
		"get_thread":             "https://news.ycombinator.com/item?id=8863",
	} {
		text, _ := callPlatformTool(t, tool, map[string]interface{}{"url": url})
		if strings.Contains(text, "only accepts") {
			t.Fatalf("%s rejected a URL of its own platform: %q", tool, text)
		}
	}
}
