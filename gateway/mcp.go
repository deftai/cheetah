package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/gofiber/fiber/v2"
)

type mcpTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
}

var mcpTools = []mcpTool{
	{
		Name:        "scrape",
		Description: "Scrape a single web page and return its content as markdown, HTML, or plain text.",
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "The URL to scrape"},
				"format": {"type": "string", "enum": ["markdown", "html", "text"], "description": "Output format (default: markdown)"}
			},
			"required": ["url"]
		}`),
	},
	{
		Name:        "crawl",
		Description: "Crawl a website starting from a URL, following links up to a configurable depth and page count.",
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "The starting URL to crawl"},
				"max_pages": {"type": "integer", "description": "Maximum number of pages to crawl (default: 10)"},
				"max_depth": {"type": "integer", "description": "Maximum link depth to follow (default: 3)"},
				"format": {"type": "string", "enum": ["markdown", "html", "text"], "description": "Output format (default: markdown)"},
				"include_patterns": {"type": "array", "items": {"type": "string"}, "description": "URL patterns to include"},
				"exclude_patterns": {"type": "array", "items": {"type": "string"}, "description": "URL patterns to exclude"}
			},
			"required": ["url"]
		}`),
	},
	{
		Name:        "get_repository_content",
		Description: "Get the content of a GitHub repository page as markdown: a repository README, a file (github.com/<owner>/<repo>/blob/<ref>/<path>), or an issue or pull request thread with its comments. Only accepts GitHub URLs; use scrape for other pages.",
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "A GitHub repository, file, issue or pull request URL"}
			},
			"required": ["url"]
		}`),
	},
	{
		Name:        "get_thread",
		Description: "Get a Hacker News story or comment thread, with its full comment tree, as markdown. Only accepts Hacker News item URLs (https://news.ycombinator.com/item?id=<id>); use scrape for other pages.",
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "A Hacker News item URL"}
			},
			"required": ["url"]
		}`),
	},
	{
		Name:        "extract",
		Description: "Extract structured data from a web page using AI. Requires a JSON schema describing the data to extract.",
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {
				"url": {"type": "string", "description": "The URL to extract data from"},
				"extract_schema": {"type": "object", "description": "JSON schema describing the structured data to extract"}
			},
			"required": ["url", "extract_schema"]
		}`),
	},
}

// transcriptTool is only offered when the transcript worker is enabled
// (Config.TranscriptWorker), so clients are not shown a tool that cannot work.
var transcriptTool = mcpTool{
	Name:        "extract_transcript",
	Description: "Get the transcript (captions) of a YouTube video as text with timestamps. Only accepts YouTube video URLs; use scrape for other pages. Best-effort: YouTube may refuse some requests, and videos without captions return an error.",
	InputSchema: json.RawMessage(`{
		"type": "object",
		"properties": {
			"url": {"type": "string", "description": "A YouTube video URL (watch, youtu.be, shorts, embed or live)"},
			"language": {"type": "string", "description": "Caption language code, e.g. en, pt-BR, or ko-orig for the original spoken language (default: the video's original language when available)"}
		},
		"required": ["url"]
	}`),
}

// mcpToolsFor returns the tools to advertise for this server's configuration.
func mcpToolsFor(transcriptEnabled bool) []mcpTool {
	if !transcriptEnabled {
		return mcpTools
	}
	return append(append([]mcpTool{}, mcpTools...), transcriptTool)
}

type jsonrpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type jsonrpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  interface{}     `json:"result,omitempty"`
	Error   *jsonrpcError   `json:"error,omitempty"`
}

type jsonrpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type mcpInitResult struct {
	ProtocolVersion string        `json:"protocolVersion"`
	Capabilities    mcpCaps       `json:"capabilities"`
	ServerInfo      mcpServerInfo `json:"serverInfo"`
}

type mcpCaps struct {
	Tools struct{} `json:"tools"`
}

type mcpServerInfo struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

type mcpToolsListResult struct {
	Tools []mcpTool `json:"tools"`
}

type mcpToolCallResult struct {
	Content []mcpContent `json:"content"`
	IsError bool         `json:"isError,omitempty"`
}

type mcpContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type toolCallParams struct {
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

func handleMCPDiscovery(transcriptEnabled bool) fiber.Handler {
	info := struct {
		Name        string    `json:"name"`
		Version     string    `json:"version"`
		Description string    `json:"description"`
		Endpoint    string    `json:"endpoint"`
		Tools       []mcpTool `json:"tools"`
	}{
		Name:        "cheetah",
		Version:     "1.0.0",
		Description: "Web scraping, crawling, and AI extraction API",
		Endpoint:    "/v1/mcp",
		Tools:       mcpToolsFor(transcriptEnabled),
	}

	return func(c *fiber.Ctx) error {
		return c.JSON(info)
	}
}

func handleMCP(queue *QueueClient, aiEndpoint string, transcriptEnabled bool) fiber.Handler {
	return func(c *fiber.Ctx) error {
		var req jsonrpcRequest
		if err := json.Unmarshal(c.Body(), &req); err != nil {
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				Error:   &jsonrpcError{Code: -32700, Message: "parse error"},
			})
		}

		if req.JSONRPC != "2.0" {
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Error:   &jsonrpcError{Code: -32600, Message: "jsonrpc must be \"2.0\""},
			})
		}

		if req.ID == nil {
			return c.SendStatus(fiber.StatusAccepted)
		}

		switch req.Method {
		case "initialize":
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result: mcpInitResult{
					ProtocolVersion: "2024-11-05",
					Capabilities:    mcpCaps{},
					ServerInfo:      mcpServerInfo{Name: "cheetah", Version: "1.0.0"},
				},
			})
		case "tools/list":
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result:  mcpToolsListResult{Tools: mcpToolsFor(transcriptEnabled)},
			})
		case "tools/call":
			return handleMCPToolCall(c, req, queue, aiEndpoint, transcriptEnabled)
		default:
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Error:   &jsonrpcError{Code: -32601, Message: "method not found"},
			})
		}
	}
}

func handleMCPToolCall(c *fiber.Ctx, req jsonrpcRequest, queue *QueueClient, aiEndpoint string, transcriptEnabled bool) error {
	var params toolCallParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return c.JSON(jsonrpcResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Error:   &jsonrpcError{Code: -32602, Message: "invalid params"},
		})
	}

	switch params.Name {
	case "scrape":
		return mcpScrape(c, req, params, queue)
	case "crawl":
		return mcpCrawl(c, req, params, queue)
	case "get_repository_content", "get_thread":
		return mcpPlatformTool(c, req, params, queue)
	case "extract_transcript":
		return mcpTranscript(c, req, params, queue, transcriptEnabled)
	case "extract":
		return mcpExtract(c, req, params, queue, aiEndpoint)
	default:
		return c.JSON(jsonrpcResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Error:   &jsonrpcError{Code: -32602, Message: "unknown tool: " + params.Name},
		})
	}
}

func mcpScrape(c *fiber.Ctx, rpc jsonrpcRequest, params toolCallParams, queue *QueueClient) error {
	var args struct {
		URL    string `json:"url"`
		Format string `json:"format"`
	}
	if err := json.Unmarshal(params.Arguments, &args); err != nil || args.URL == "" {
		return mcpToolError(c, rpc, "url is required")
	}

	resolvedIP, err := ValidateScrapeURL(args.URL)
	if err != nil {
		return mcpToolError(c, rpc, err.Error())
	}

	if args.Format == "" {
		args.Format = "markdown"
	}
	if !validateFormat(args.Format) {
		return mcpToolError(c, rpc, "format must be markdown, html, or text")
	}

	decision := ClassifyURL(args.URL)
	plan, _ := c.Locals("plan").(Plan)
	if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
		return mcpToolError(c, rpc, "this URL requires the "+string(decision.Tier)+" tier, upgrade your plan")
	}

	owner := callerOwner(c)
	stream := StreamForTier(decision.Tier)
	scrapeReq := ScrapeRequest{URL: args.URL, Format: args.Format, ResolvedIP: resolvedIP, Strategy: decision.Strategy}
	jobID, err := queue.EnqueueScrape(c.Context(), scrapeReq, owner, stream)
	if err != nil {
		log.Printf("mcp enqueue scrape error: %v", err)
		return mcpToolError(c, rpc, "failed to enqueue job")
	}

	LogRouteDecision(jobID, args.URL, decision)
	return mcpToolSuccess(c, rpc, map[string]interface{}{"success": true, "job_id": jobID})
}

// platformTool describes an MCP tool that serves one platform's URLs through
// that platform's extractor.
type platformTool struct {
	strategy string // the router strategy the URL must classify as
	accepts  string // what the tool accepts, for the error message
}

var platformTools = map[string]platformTool{
	"get_repository_content": {
		strategy: StrategyGitHubRepo,
		accepts:  "a GitHub repository, file, issue or pull request URL (github.com/<owner>/<repo>[/blob/<ref>/<path> | /issues/<n> | /pull/<n>])",
	},
	"get_thread": {
		strategy: StrategyHNThread,
		accepts:  "a Hacker News item URL (https://news.ycombinator.com/item?id=<id>)",
	},
}

// platformToolOrder fixes the order platformTools is searched in, so error
// hints are deterministic.
var platformToolOrder = []string{"get_repository_content", "get_thread"}

func isDigits(s string, maxLen int) bool {
	if s == "" || len(s) > maxLen {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func validGitHubName(s string) bool {
	if s == "" || len(s) > 100 || s == "." || s == ".." {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' || c == '_' || c == '-') {
			return false
		}
	}
	return true
}

// decodeRefSegment mirrors the worker's `decode_segment` for a blob ref or path
// segment: percent-decode, then refuse anything that could escape its position
// (empty, dot segments, separators, control characters, invalid UTF-8).
func decodeRefSegment(raw string) (string, bool) {
	dec, err := url.PathUnescape(raw)
	if err != nil || dec == "" || dec == "." || dec == ".." || !utf8.ValidString(dec) {
		return "", false
	}
	for _, r := range dec {
		if r == '/' || r == '\\' || unicode.IsControl(r) {
			return "", false
		}
	}
	return dec, true
}

// rawSegments splits a still-encoded path into its non-empty segments. The
// worker parses the raw path (a `%2F` is one segment, not two), so the gateway
// must too. A segment that is a dot segment in any spelling (`.`, `..`, `%2e`),
// or a path with a backslash (the worker's URL parser treats it as `/`), is
// reported as unusable rather than normalised: stricter than the worker, never
// looser.
func rawSegments(escapedPath string) ([]string, bool) {
	if strings.Contains(escapedPath, `\`) {
		return nil, false
	}
	var segs []string
	for _, p := range strings.Split(escapedPath, "/") {
		if p == "" {
			continue
		}
		if dec, err := url.PathUnescape(p); err != nil || dec == "." || dec == ".." {
			return nil, false
		}
		segs = append(segs, p)
	}
	return segs, true
}

// rawPathOf returns the path of an https URL exactly as written: everything
// after the authority up to the first `?` or `#`. Go's `URL.EscapedPath` is not
// used because it discards the written form (turning `%2F` into `/`) whenever
// the path holds a character Go would itself escape, such as `|` or non-ASCII.
func rawPathOf(rawURL string) (string, bool) {
	const prefix = "https://"
	if len(rawURL) < len(prefix) || !strings.EqualFold(rawURL[:len(prefix)], prefix) {
		return "", false
	}
	rest := rawURL[len(prefix):]
	i := strings.IndexAny(rest, "/?#")
	if i < 0 || rest[i] != '/' {
		return "", true
	}
	path := rest[i:]
	if j := strings.IndexAny(path, "?#"); j >= 0 {
		path = path[:j]
	}
	return path, true
}

// platformURLSupported reports whether rawURL is a page the strategy's
// extractor handles. It mirrors the worker's own URL rules (workers/fast
// extractors), so a URL the worker would only fall back on is refused here
// with a clear error instead of being reported as extracted. Where the two
// could differ it errs on the strict side.
func platformURLSupported(strategy, rawURL string) bool {
	// The worker's URL parser strips surrounding whitespace and control
	// characters (WHATWG), Go's does not, so `.../dir/ ` would be a directory to
	// the worker but a file name here. A legitimate URL has none, only `%20`.
	for _, r := range rawURL {
		if unicode.IsSpace(r) || unicode.IsControl(r) {
			return false
		}
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" {
		return false
	}
	if port := u.Port(); port != "" {
		if n, err := strconv.ParseUint(port, 10, 32); err != nil || n > 65535 {
			return false
		}
	}
	host := strings.ToLower(u.Hostname())
	escaped, ok := rawPathOf(rawURL)
	if !ok {
		return false
	}
	switch strategy {
	case StrategyGitHubRepo:
		if host != "github.com" && host != "www.github.com" {
			return false
		}
		segs, ok := rawSegments(escaped)
		// Owner and repo are never percent-encoded in the worker's rules.
		if !ok || len(segs) < 2 || !validGitHubName(segs[0]) || !validGitHubName(strings.TrimSuffix(segs[1], ".git")) {
			return false
		}
		rest := segs[2:]
		switch {
		case len(rest) == 0:
			return true
		case rest[0] == "blob":
			// /blob/<ref>/<path...>; a trailing slash names a directory.
			if len(rest) < 3 || strings.HasSuffix(escaped, "/") {
				return false
			}
			for _, seg := range rest[1:] {
				if _, ok := decodeRefSegment(seg); !ok {
					return false
				}
			}
			return true
		case (rest[0] == "issues" || rest[0] == "pull") && len(rest) == 2:
			_, err := strconv.ParseUint(rest[1], 10, 64)
			return isDigits(rest[1], 20) && err == nil
		}
		return false
	case StrategyHNThread:
		if host != "news.ycombinator.com" || escaped != "/item" {
			return false
		}
		q, err := url.ParseQuery(u.RawQuery)
		if err != nil {
			return false
		}
		ids := q["id"]
		return len(ids) > 0 && isDigits(ids[0], 10)
	}
	return false
}

// platformToolError returns "" when the URL is one the tool serves, and
// otherwise a message saying what the tool accepts and where the URL belongs.
// It never repeats the submitted URL, which may carry credentials in its query.
func platformToolError(tool, rawURL string, decision RouteDecision) string {
	spec := platformTools[tool]
	if decision.Strategy == spec.strategy {
		if platformURLSupported(spec.strategy, rawURL) {
			return ""
		}
		return fmt.Sprintf("%s only accepts %s; this page type or URL form is not supported, use \"scrape\" for it", tool, spec.accepts)
	}
	hint := `use "scrape" for other pages`
	for _, other := range platformToolOrder {
		if other != tool && decision.Strategy == platformTools[other].strategy {
			hint = fmt.Sprintf("this URL belongs to %q", other)
			break
		}
	}
	return fmt.Sprintf("%s only accepts %s; %s", tool, spec.accepts, hint)
}

func mcpPlatformTool(c *fiber.Ctx, rpc jsonrpcRequest, params toolCallParams, queue *QueueClient) error {
	var args struct {
		URL string `json:"url"`
	}
	if err := json.Unmarshal(params.Arguments, &args); err != nil || args.URL == "" {
		return mcpToolError(c, rpc, "url is required")
	}

	// The platform check is a pure classification, so a wrong-platform URL is
	// refused before any DNS lookup or queue access.
	decision := ClassifyURL(args.URL)
	if msg := platformToolError(params.Name, args.URL, decision); msg != "" {
		return mcpToolError(c, rpc, msg)
	}

	resolvedIP, err := ValidateScrapeURL(args.URL)
	if err != nil {
		return mcpToolError(c, rpc, err.Error())
	}

	plan, _ := c.Locals("plan").(Plan)
	if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
		return mcpToolError(c, rpc, "this URL requires the "+string(decision.Tier)+" tier, upgrade your plan")
	}

	// Extractors produce markdown, so the format is fixed; the strategy is the
	// one the URL already classified as, so the worker runs the right extractor
	// and falls back to a plain fetch if it cannot.
	scrapeReq := ScrapeRequest{URL: args.URL, Format: "markdown", ResolvedIP: resolvedIP, Strategy: decision.Strategy}
	jobID, err := queue.EnqueueScrape(c.Context(), scrapeReq, callerOwner(c), StreamForTier(decision.Tier))
	if err != nil {
		log.Printf("mcp enqueue %s error: %v", params.Name, err)
		return mcpToolError(c, rpc, "failed to enqueue job")
	}

	LogRouteDecision(jobID, args.URL, decision)
	return mcpToolSuccess(c, rpc, map[string]interface{}{"success": true, "job_id": jobID, "strategy": decision.Strategy})
}

func mcpTranscript(c *fiber.Ctx, rpc jsonrpcRequest, params toolCallParams, queue *QueueClient, enabled bool) error {
	var args struct {
		URL      string `json:"url"`
		Language string `json:"language"`
	}
	if err := json.Unmarshal(params.Arguments, &args); err != nil || args.URL == "" {
		return mcpToolError(c, rpc, "url is required")
	}
	if !enabled {
		return mcpToolError(c, rpc, "extract_transcript is not enabled on this server")
	}
	if args.Language != "" && !validTranscriptLanguage(args.Language) {
		return mcpToolError(c, rpc, "language must be a language code such as en, pt-BR or ko-orig")
	}

	// A pure check, so a wrong URL is refused before any DNS lookup or queue access.
	videoID, msg := transcriptURLError(args.URL)
	if msg != "" {
		return mcpToolError(c, rpc, msg)
	}

	// Only the validated id is used: the worker is given the canonical URL, never
	// the submitted one.
	canonical := canonicalYouTubeURL(videoID)
	resolvedIP, err := ValidateScrapeURL(canonical)
	if err != nil {
		return mcpToolError(c, rpc, err.Error())
	}

	plan, _ := c.Locals("plan").(Plan)
	if !IsTierAllowed(TierFast, plan.AllowedTiers) {
		return mcpToolError(c, rpc, "this URL requires the fast tier, upgrade your plan")
	}

	scrapeReq := ScrapeRequest{
		URL:        canonical,
		Format:     "markdown",
		ResolvedIP: resolvedIP,
		Strategy:   StrategyYouTubeTranscript,
		VideoID:    videoID,
		Language:   args.Language,
	}
	jobID, err := queue.EnqueueScrape(c.Context(), scrapeReq, callerOwner(c), transcriptStream)
	if err != nil {
		log.Printf("mcp enqueue transcript error: %v", err)
		return mcpToolError(c, rpc, "failed to enqueue job")
	}

	LogRouteDecision(jobID, canonical, RouteDecision{
		Tier:       TierFast,
		Reason:     "platform: " + StrategyYouTubeTranscript,
		Confidence: 0.9,
		Strategy:   StrategyYouTubeTranscript,
	})
	return mcpToolSuccess(c, rpc, map[string]interface{}{
		"success":  true,
		"job_id":   jobID,
		"strategy": StrategyYouTubeTranscript,
		"video_id": videoID,
	})
}

func mcpCrawl(c *fiber.Ctx, rpc jsonrpcRequest, params toolCallParams, queue *QueueClient) error {
	var args struct {
		URL             string   `json:"url"`
		MaxPages        int      `json:"max_pages"`
		MaxDepth        int      `json:"max_depth"`
		Format          string   `json:"format"`
		IncludePatterns []string `json:"include_patterns"`
		ExcludePatterns []string `json:"exclude_patterns"`
	}
	if err := json.Unmarshal(params.Arguments, &args); err != nil || args.URL == "" {
		return mcpToolError(c, rpc, "url is required")
	}

	resolvedIP, err := ValidateScrapeURL(args.URL)
	if err != nil {
		return mcpToolError(c, rpc, err.Error())
	}

	if args.MaxPages <= 0 {
		args.MaxPages = 10
	}
	plan, _ := c.Locals("plan").(Plan)
	if plan.MaxCrawlPages > 0 && args.MaxPages > plan.MaxCrawlPages {
		args.MaxPages = plan.MaxCrawlPages
	}
	if args.MaxPages > 1000 {
		args.MaxPages = 1000
	}

	if args.MaxDepth <= 0 {
		args.MaxDepth = 3
	}
	if args.MaxDepth > 10 {
		args.MaxDepth = 10
	}

	if args.Format == "" {
		args.Format = "markdown"
	}
	if !validateFormat(args.Format) {
		return mcpToolError(c, rpc, "format must be markdown, html, or text")
	}

	decision := ClassifyURL(args.URL)
	if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
		return mcpToolError(c, rpc, "this URL requires the "+string(decision.Tier)+" tier, upgrade your plan")
	}

	crawlReq := CrawlRequest{
		URL:             args.URL,
		MaxPages:        args.MaxPages,
		MaxDepth:        args.MaxDepth,
		MaxTimeout:      300,
		Format:          args.Format,
		IncludePatterns: args.IncludePatterns,
		ExcludePatterns: args.ExcludePatterns,
		ResolvedIP:      resolvedIP,
		ResultTTL:       planResultTTL(plan),
	}

	owner := callerOwner(c)
	jobID, err := queue.EnqueueCrawl(c.Context(), crawlReq, owner)
	if err != nil {
		log.Printf("mcp enqueue crawl error: %v", err)
		return mcpToolError(c, rpc, "failed to enqueue job")
	}

	LogRouteDecision(jobID, args.URL, decision)
	return mcpToolSuccess(c, rpc, map[string]interface{}{"success": true, "job_id": jobID, "status": "queued"})
}

func mcpExtract(c *fiber.Ctx, rpc jsonrpcRequest, params toolCallParams, queue *QueueClient, aiEndpoint string) error {
	if aiEndpoint == "" {
		return mcpToolError(c, rpc, "extraction service not configured")
	}

	plan, _ := c.Locals("plan").(Plan)
	if !plan.CanExtract {
		return mcpToolError(c, rpc, "extract is not available on your current plan")
	}

	var args struct {
		URL           string          `json:"url"`
		ExtractSchema json.RawMessage `json:"extract_schema"`
	}
	if err := json.Unmarshal(params.Arguments, &args); err != nil || args.URL == "" {
		return mcpToolError(c, rpc, "url is required")
	}

	resolvedIP, err := ValidateScrapeURL(args.URL)
	if err != nil {
		return mcpToolError(c, rpc, err.Error())
	}

	if len(args.ExtractSchema) == 0 {
		return mcpToolError(c, rpc, "extract_schema is required")
	}
	if !json.Valid(args.ExtractSchema) {
		return mcpToolError(c, rpc, "extract_schema must be valid JSON")
	}

	decision := ClassifyURL(args.URL)
	if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
		return mcpToolError(c, rpc, "this URL requires the "+string(decision.Tier)+" tier, upgrade your plan")
	}

	scrapeReq := ScrapeRequest{
		URL:           args.URL,
		Format:        "markdown",
		ExtractSchema: (*json.RawMessage)(&args.ExtractSchema),
		ResolvedIP:    resolvedIP,
		Strategy:      decision.Strategy,
	}

	owner := callerOwner(c)
	stream := StreamForTier(decision.Tier)
	jobID, err := queue.EnqueueScrape(c.Context(), scrapeReq, owner, stream)
	if err != nil {
		log.Printf("mcp enqueue extract error: %v", err)
		return mcpToolError(c, rpc, "failed to enqueue job")
	}

	LogRouteDecision(jobID, args.URL, decision)
	return mcpToolSuccess(c, rpc, map[string]interface{}{"success": true, "job_id": jobID})
}

func mcpToolSuccess(c *fiber.Ctx, rpc jsonrpcRequest, data interface{}) error {
	text, _ := json.Marshal(data)
	return c.JSON(jsonrpcResponse{
		JSONRPC: "2.0",
		ID:      rpc.ID,
		Result: mcpToolCallResult{
			Content: []mcpContent{{Type: "text", Text: string(text)}},
		},
	})
}

func mcpToolError(c *fiber.Ctx, rpc jsonrpcRequest, msg string) error {
	return c.JSON(jsonrpcResponse{
		JSONRPC: "2.0",
		ID:      rpc.ID,
		Result: mcpToolCallResult{
			Content: []mcpContent{{Type: "text", Text: msg}},
			IsError: true,
		},
	})
}
