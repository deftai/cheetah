package main

import (
	"encoding/json"
	"fmt"
	"log"

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

func handleMCPDiscovery() fiber.Handler {
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
		Tools:       mcpTools,
	}

	return func(c *fiber.Ctx) error {
		return c.JSON(info)
	}
}

func handleMCP(queue *QueueClient, aiEndpoint string) fiber.Handler {
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
				Result:  mcpToolsListResult{Tools: mcpTools},
			})
		case "tools/call":
			return handleMCPToolCall(c, req, queue, aiEndpoint)
		default:
			return c.JSON(jsonrpcResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Error:   &jsonrpcError{Code: -32601, Message: "method not found"},
			})
		}
	}
}

func handleMCPToolCall(c *fiber.Ctx, req jsonrpcRequest, queue *QueueClient, aiEndpoint string) error {
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

// platformToolError returns "" when decision is a URL the tool serves, and
// otherwise a message saying what the tool accepts and where the URL belongs.
// It never repeats the submitted URL, which may carry credentials in its query.
func platformToolError(tool string, decision RouteDecision) string {
	spec := platformTools[tool]
	if decision.Strategy == spec.strategy {
		return ""
	}
	hint := `use "scrape" for other pages`
	for other, o := range platformTools {
		if other != tool && decision.Strategy == o.strategy {
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
	if msg := platformToolError(params.Name, decision); msg != "" {
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
