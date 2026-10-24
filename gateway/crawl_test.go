package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestExtractLinks(t *testing.T) {
	htmlContent := `<html><body>
		<a href="/page1">Page 1</a>
		<a href="/page2">Page 2</a>
		<a href="https://other.com/ext">External</a>
		<a href="#fragment">Fragment</a>
		<a href="javascript:void(0)">JS</a>
		<a href="mailto:test@test.com">Mail</a>
		<a href="">Empty</a>
	</body></html>`

	links := extractLinks(htmlContent, "https://example.com/start")

	expected := map[string]bool{
		"https://example.com/page1": false,
		"https://example.com/page2": false,
		"https://other.com/ext":     false,
	}

	for _, link := range links {
		if _, ok := expected[link]; ok {
			expected[link] = true
		}
	}

	for link, found := range expected {
		if !found {
			t.Errorf("expected link %q not found", link)
		}
	}

	for _, link := range links {
		if strings.HasPrefix(link, "#") || strings.HasPrefix(link, "javascript:") || strings.HasPrefix(link, "mailto:") || link == "" {
			t.Errorf("unexpected link %q should have been filtered", link)
		}
	}
}

// Only http(s) links may come out of extractLinks. A blocklist of schemes (javascript:,
// mailto:, tel:) misses data:, vbscript:, mixed-case spellings and anything else, so the
// check is an allowlist on the resolved URL.
func TestExtractLinksKeepsOnlyHTTPLinks(t *testing.T) {
	htmlContent := `<html><body>
		<a href="/ok">relative</a>
		<a href="//cdn.example.com/p">protocol-relative</a>
		<a href="HTTPS://example.com/upper">upper-case scheme</a>
		<a href="http://example.com/plain">http</a>
		<a href="data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;">data</a>
		<a href="vbscript:msgbox(1)">vbscript</a>
		<a href="JavaScript:alert(1)">mixed-case javascript</a>
		<a href=" javascript:alert(1)">leading space</a>
		<a href="java&#9;script:alert(1)">tab inside</a>
		<a href="ftp://example.com/file">ftp</a>
		<a href="file:///etc/passwd">file</a>
		<a href="mailto:a@b.c">mail</a>
		<a href="tel:+15550100">tel</a>
		<a href="blob:https://example.com/uuid">blob</a>
		<a href="ws://example.com/socket">ws</a>
	</body></html>`

	want := map[string]bool{
		"https://example.com/ok":    true,
		"https://cdn.example.com/p": true,
		"https://example.com/upper": true,
		"http://example.com/plain":  true,
	}

	links := extractLinks(htmlContent, "https://example.com/start")
	for _, link := range links {
		if !want[link] {
			t.Errorf("link %q should have been dropped", link)
		}
		delete(want, link)
	}
	for link := range want {
		t.Errorf("expected link %q was dropped", link)
	}
}

func TestExtractLinksRelative(t *testing.T) {
	htmlContent := `<html><body>
		<a href="sub/page">Relative</a>
		<a href="../other">Parent</a>
	</body></html>`

	links := extractLinks(htmlContent, "https://example.com/docs/intro")

	hasSubPage := false
	for _, link := range links {
		if link == "https://example.com/docs/sub/page" {
			hasSubPage = true
		}
	}
	if !hasSubPage {
		t.Errorf("expected resolved relative link, got %v", links)
	}
}

func TestFilterLinksSameDomain(t *testing.T) {
	seed, _ := url.Parse("https://example.com/start")
	links := []string{
		"https://example.com/page1",
		"https://example.com/page2",
		"https://other.com/page",
		"ftp://example.com/file",
	}

	filtered := filterLinks(links, seed, nil, nil)

	if len(filtered) != 2 {
		t.Fatalf("expected 2 same-domain links, got %d: %v", len(filtered), filtered)
	}
}

func TestFilterLinksIncludePatterns(t *testing.T) {
	seed, _ := url.Parse("https://example.com")
	links := []string{
		"https://example.com/docs/intro",
		"https://example.com/blog/post",
		"https://example.com/docs/guide",
	}

	filtered := filterLinks(links, seed, []string{"/docs/"}, nil)

	if len(filtered) != 2 {
		t.Fatalf("expected 2 matching links, got %d: %v", len(filtered), filtered)
	}
}

func TestFilterLinksExcludePatterns(t *testing.T) {
	seed, _ := url.Parse("https://example.com")
	links := []string{
		"https://example.com/docs/intro",
		"https://example.com/login",
		"https://example.com/docs/guide",
	}

	filtered := filterLinks(links, seed, nil, []string{"/login"})

	if len(filtered) != 2 {
		t.Fatalf("expected 2 links after exclusion, got %d: %v", len(filtered), filtered)
	}
}

func TestFilterLinksSkipsExtensions(t *testing.T) {
	seed, _ := url.Parse("https://example.com")
	links := []string{
		"https://example.com/page",
		"https://example.com/doc.pdf",
		"https://example.com/image.png",
		"https://example.com/style.css",
		"https://example.com/app.js",
		"https://example.com/about",
	}

	filtered := filterLinks(links, seed, nil, nil)

	if len(filtered) != 2 {
		t.Fatalf("expected 2 non-asset links, got %d: %v", len(filtered), filtered)
	}
}

func TestNormalizeURL(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"https://example.com/page#section", "https://example.com/page"},
		{"https://example.com/page?q=1", "https://example.com/page?q=1"},
		{"https://example.com/path/", "https://example.com/path"},
		{"https://example.com", "https://example.com"},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := normalizeURL(tt.input)
			if got != tt.want {
				t.Fatalf("normalizeURL(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestExtractTitle(t *testing.T) {
	tests := []struct {
		html string
		want string
	}{
		{`<html><head><title>Hello World</title></head></html>`, "Hello World"},
		{`<html><head><title>  Trimmed  </title></head></html>`, "Trimmed"},
		{`<html><body>No title</body></html>`, ""},
	}

	for _, tt := range tests {
		t.Run(tt.want, func(t *testing.T) {
			got := extractTitle(tt.html)
			if got != tt.want {
				t.Fatalf("extractTitle = %q, want %q", got, tt.want)
			}
		})
	}
}

func testCrawlManager(client *http.Client) *CrawlManager {
	return &CrawlManager{
		httpClient: client,
		queue:      NewQueueClient("localhost:6379"),
	}
}

func TestCrawlManagerDepthEnforcement(t *testing.T) {
	pages := map[string]string{
		"/":       `<html><head><title>Root</title></head><body><a href="/level1">L1</a></body></html>`,
		"/level1": `<html><head><title>Level 1</title></head><body><a href="/level2">L2</a></body></html>`,
		"/level2": `<html><head><title>Level 2</title></head><body><a href="/level3">L3</a></body></html>`,
		"/level3": `<html><head><title>Level 3</title></head><body><a href="/level4">L4</a></body></html>`,
		"/level4": `<html><head><title>Level 4</title></head><body>End</body></html>`,
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, ok := pages[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, body)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())

	req := CrawlRequest{
		URL:        srv.URL + "/",
		MaxPages:   100,
		MaxDepth:   2,
		MaxTimeout: 10,
		Format:     "markdown",
	}

	done := make(chan struct{})
	go func() {
		cm.RunCrawl(context.Background(), "test-depth", req)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("crawl timed out")
	}

	val, err := cm.queue.rdb.Get(context.Background(), jobKey("test-depth")).Result()
	if err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		t.Fatal(err)
	}

	if status.PagesTotal > 3 {
		t.Fatalf("expected at most 3 pages (depth 0,1,2), got %d", status.PagesTotal)
	}

	for _, r := range status.Results {
		if r.Data.Metadata.Title == "Level 3" || r.Data.Metadata.Title == "Level 4" {
			t.Fatalf("should not have crawled beyond depth 2, found %q", r.Data.Metadata.Title)
		}
	}
}

func TestCrawlManagerMaxPagesEnforcement(t *testing.T) {
	pageCount := 20
	linkHTML := "<html><head><title>Links</title></head><body>"
	for i := 1; i <= pageCount; i++ {
		linkHTML += fmt.Sprintf(`<a href="/page%d">Page %d</a>`, i, i)
	}
	linkHTML += "</body></html>"

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		if r.URL.Path == "/" {
			fmt.Fprint(w, linkHTML)
		} else {
			fmt.Fprintf(w, "<html><head><title>%s</title></head><body>Content</body></html>", r.URL.Path)
		}
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())

	req := CrawlRequest{
		URL:        srv.URL + "/",
		MaxPages:   5,
		MaxDepth:   3,
		MaxTimeout: 10,
		Format:     "markdown",
	}

	done := make(chan struct{})
	go func() {
		cm.RunCrawl(context.Background(), "test-pages", req)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("crawl timed out")
	}

	val, err := cm.queue.rdb.Get(context.Background(), jobKey("test-pages")).Result()
	if err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		t.Fatal(err)
	}

	if status.PagesTotal > 5 {
		t.Fatalf("expected at most 5 pages, got %d", status.PagesTotal)
	}
}

func TestCrawlManagerTimeoutReturnsPartial(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			time.Sleep(2 * time.Second)
		}
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, `<html><head><title>Slow</title></head><body>
			<a href="/slow1">S1</a><a href="/slow2">S2</a>
			<a href="/slow3">S3</a><a href="/slow4">S4</a>
		</body></html>`)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())

	req := CrawlRequest{
		URL:        srv.URL + "/",
		MaxPages:   10,
		MaxDepth:   3,
		MaxTimeout: 1,
		Format:     "markdown",
	}

	done := make(chan struct{})
	go func() {
		cm.RunCrawl(context.Background(), "test-timeout", req)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("crawl did not complete")
	}

	val, err := cm.queue.rdb.Get(context.Background(), jobKey("test-timeout")).Result()
	if err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		t.Fatal(err)
	}

	if status.Status != "partial" {
		t.Fatalf("expected status 'partial' for timed-out crawl, got %q", status.Status)
	}
}

func TestCrawlManagerContextCancellation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, `<html><head><title>Slow</title></head><body>
			<a href="/page1">P1</a><a href="/page2">P2</a>
		</body></html>`)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())

	req := CrawlRequest{
		URL:        srv.URL + "/",
		MaxPages:   100,
		MaxDepth:   5,
		MaxTimeout: 30,
		Format:     "markdown",
	}

	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		cm.RunCrawl(ctx, "test-cancel", req)
		close(done)
	}()

	time.Sleep(100 * time.Millisecond)
	cancel()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("crawl did not stop after cancellation")
	}

	val, err := cm.queue.rdb.Get(context.Background(), jobKey("test-cancel")).Result()
	if err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		t.Fatal(err)
	}

	if status.Status != "partial" {
		t.Fatalf("expected status 'partial' for cancelled crawl, got %q", status.Status)
	}
}

func TestCrawlRedirectToPrivateBlocked(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://127.0.0.1/secret", http.StatusFound)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())
	result := cm.fetchPage(context.Background(), srv.URL+"/redir")

	if result.Error == "" {
		t.Fatal("expected error for redirect to private IP")
	}
}

func TestCrawlMaxDepthValidation(t *testing.T) {
	app := testApp()

	body := `{"url": "https://example.com", "max_depth": 50}`
	req := httptest.NewRequest(http.MethodPost, "/v1/crawl", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", "test-key-123")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode == http.StatusBadRequest {
		t.Fatal("max_depth=50 should not be rejected; it should be capped silently")
	}
}

func TestCrawlMaxTimeoutValidation(t *testing.T) {
	app := testApp()

	body := `{"url": "https://example.com", "max_timeout": 9999}`
	req := httptest.NewRequest(http.MethodPost, "/v1/crawl", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", "test-key-123")

	resp, err := app.Test(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode == http.StatusBadRequest {
		t.Fatal("max_timeout=9999 should not be rejected; it should be capped silently")
	}
}

func TestCrawlPagesCompletedConsistent(t *testing.T) {
	pages := map[string]string{
		"/":      `<html><head><title>Root</title></head><body><a href="/p1">P1</a><a href="/p2">P2</a></body></html>`,
		"/p1":    `<html><head><title>P1</title></head><body>Content</body></html>`,
		"/p2":    `<html><head><title>P2</title></head><body>Content</body></html>`,
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, ok := pages[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, body)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())

	req := CrawlRequest{
		URL:        srv.URL + "/",
		MaxPages:   10,
		MaxDepth:   3,
		MaxTimeout: 10,
		Format:     "markdown",
	}

	done := make(chan struct{})
	go func() {
		cm.RunCrawl(context.Background(), "test-consistent", req)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("crawl timed out")
	}

	val, err := cm.queue.rdb.Get(context.Background(), jobKey("test-consistent")).Result()
	if err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		t.Fatal(err)
	}

	if status.PagesCompleted != status.PagesTotal {
		t.Fatalf("PagesCompleted (%d) should equal PagesTotal (%d) on finalization",
			status.PagesCompleted, status.PagesTotal)
	}
}
func TestFetchPageReturnsErrorForHTTP404(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	cm := testCrawlManager(srv.Client())
	result := cm.fetchPage(context.Background(), srv.URL+"/missing")

	if result.Error == "" {
		t.Fatal("expected error for 404 response")
	}
	if result.StatusCode != 404 {
		t.Fatalf("expected status 404, got %d", result.StatusCode)
	}
	if result.Content != "" {
		t.Fatal("expected no content for error responses")
	}
}

func TestFetchPageSanitizesErrors(t *testing.T) {
	cm := &CrawlManager{
		httpClient: newCrawlHTTPClient(),
		queue:      NewQueueClient("localhost:6379"),
	}
	result := cm.fetchPage(context.Background(), "http://192.0.2.1:1/unreachable")

	if result.Error == "" {
		t.Fatal("expected error for unreachable host")
	}
	if result.Error != "fetch failed" {
		t.Fatalf("expected sanitized error \"fetch failed\", got %q", result.Error)
	}
}
