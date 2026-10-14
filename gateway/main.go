package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gofiber/fiber/v2"
)

type Config struct {
	RedisURL      string
	APIKeys       []string
	Port          string
	AIEndpointURL string
	ProxySecret   string
	// TranscriptWorker says a worker is consuming transcript_jobs. Off by
	// default: the YouTube transcript tool is only offered when it is on. It is
	// an operator assertion; nothing verifies that a worker is actually running.
	TranscriptWorker bool
}

// loadProxySecret reads ENGINE_PROXY_SECRET and refuses to start on a weak
// value: an unset secret just disables the feature, but a short one invites
// offline brute force from a captured header set.
func loadProxySecret() string {
	secret := strings.TrimSpace(os.Getenv("ENGINE_PROXY_SECRET"))
	switch {
	case secret == "":
		log.Printf("ENGINE_PROXY_SECRET unset: trusted proxy plan headers disabled")
	case len(secret) < minProxySecretLen:
		log.Fatalf("ENGINE_PROXY_SECRET must be at least %d characters", minProxySecretLen)
	}
	return secret
}

// loadTranscriptWorker reads TRANSCRIPT_WORKER_ENABLED. Anything that is not a
// clear "true" leaves the feature off, and a malformed value says so in the log.
func loadTranscriptWorker() bool {
	v := strings.TrimSpace(os.Getenv("TRANSCRIPT_WORKER_ENABLED"))
	if v == "" {
		return false
	}
	enabled, err := strconv.ParseBool(v)
	if err != nil {
		log.Printf("TRANSCRIPT_WORKER_ENABLED=%q is not a boolean: YouTube transcripts stay disabled", v)
		return false
	}
	return enabled
}

func LoadConfig() Config {
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		redisURL = "localhost:6379"
	}

	port := os.Getenv("PORT")
	if port == "" {
		port = "3000"
	}

	var apiKeys []string
	keysEnv := os.Getenv("CHEETAH_API_KEYS")
	if keysEnv != "" {
		for _, key := range strings.Split(keysEnv, ",") {
			k := strings.TrimSpace(key)
			if k != "" {
				apiKeys = append(apiKeys, k)
			}
		}
	}

	return Config{
		RedisURL:      redisURL,
		APIKeys:       apiKeys,
		Port:          port,
		AIEndpointURL: os.Getenv("AI_ENDPOINT_URL"),
		ProxySecret:   loadProxySecret(),

		TranscriptWorker: loadTranscriptWorker(),
	}
}

func main() {
	cfg := LoadConfig()
	queue := NewQueueClient(cfg.RedisURL)
	defer queue.Close()

	app := NewApp(cfg, queue)
	log.Fatal(app.Listen(":" + cfg.Port))
}

func NewApp(cfg Config, queue *QueueClient, opts ...AppOption) *fiber.App {
	deps := defaultDeps()
	for _, o := range opts {
		o(&deps)
	}
	app := fiber.New(fiber.Config{
		AppName: "Cheetah API Gateway",
		// Fly's .internal (6PN) addresses are IPv6-only; Fiber defaults to tcp4.
		Network:      fiber.NetworkTCP,
		ErrorHandler: customErrorHandler,
	})

	app.Use(RequestLogger())

	app.Get("/health", handleHealthLiveness(queue))
	app.Get("/.well-known/mcp.json", handleMCPDiscovery(cfg.TranscriptWorker))

	v1 := app.Group("/v1")
	if len(cfg.APIKeys) > 0 {
		v1.Use(APIKeyAuth(cfg.APIKeys))
	} else {
		v1.Use(func(c *fiber.Ctx) error {
			c.Locals("api_key", "anonymous")
			return c.Next()
		})
	}
	v1.Use(OwnerHashMiddleware())
	v1.Use(TrustedProxyPlan(cfg.ProxySecret))
	v1.Use(PlanMiddleware(deps.planLoader))
	v1.Use(RateLimiter())
	v1.Use(func(c *fiber.Ctx) error {
		c.Locals("usage_reporter", deps.usageReporter)
		return c.Next()
	})

	v1.Get("/health", handleHealthDiagnostic(queue))
	v1.Post("/scrape", handleScrape(queue))
	v1.Post("/crawl", handleCrawl(queue))
	v1.Post("/extract", handleExtract(queue, cfg.AIEndpointURL))
	v1.Post("/mcp", handleMCP(queue, cfg.AIEndpointURL, cfg.TranscriptWorker))
	v1.Get("/crawl/:id", handleCrawlStatus(queue))

	return app
}

func customErrorHandler(c *fiber.Ctx, err error) error {
	code := fiber.StatusInternalServerError
	msg := "internal server error"
	if e, ok := err.(*fiber.Error); ok {
		code = e.Code
		msg = e.Message
	} else {
		log.Printf("unhandled error: %v", err)
	}
	return c.Status(code).JSON(ErrorResponse{
		Success: false,
		Error:   msg,
	})
}

const (
	maxResultRetentionHours = 720
	maxResultRetention      = maxResultRetentionHours * time.Hour
)

func planResultTTL(plan Plan) time.Duration {
	h := plan.ResultRetentionHours
	if h <= 0 {
		return 0
	}
	if h > maxResultRetentionHours {
		h = maxResultRetentionHours
	}
	return time.Duration(h) * time.Hour
}

func callerOwner(c *fiber.Ctx) string {
	hash, _ := c.Locals("owner_hash").(string)
	if hash == "" {
		return "anonymous"
	}
	return hash
}

func validateFormat(format string) bool {
	return format == "markdown" || format == "html" || format == "text"
}

func handleScrape(queue *QueueClient) fiber.Handler {
	return func(c *fiber.Ctx) error {
		var req ScrapeRequest
		if err := c.BodyParser(&req); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "invalid request body",
			})
		}

		if req.URL == "" {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "url is required",
			})
		}

		resolvedIP, err := ValidateScrapeURL(req.URL)
		if err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   err.Error(),
			})
		}
		req.ResolvedIP = resolvedIP

		if req.Format == "" {
			req.Format = "markdown"
		}
		if !validateFormat(req.Format) {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "format must be markdown, html, or text",
			})
		}

		decision := ClassifyURL(req.URL)

		plan, _ := c.Locals("plan").(Plan)
		if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
			return c.Status(fiber.StatusForbidden).JSON(ErrorResponse{
				Success: false,
				Error:   "this URL requires the " + string(decision.Tier) + " tier, upgrade your plan",
			})
		}

		req.Strategy = decision.Strategy
		owner := callerOwner(c)
		stream := StreamForTier(decision.Tier)
		jobID, err := queue.EnqueueScrape(c.Context(), req, owner, stream)
		if err != nil {
			log.Printf("enqueue scrape error: %v", err)
			return c.Status(fiber.StatusInternalServerError).JSON(ErrorResponse{
				Success: false,
				Error:   "failed to enqueue job",
			})
		}

		LogRouteDecision(jobID, req.URL, decision)

		return c.Status(fiber.StatusAccepted).JSON(EnqueueResponse{
			Success: true,
			JobID:   jobID,
		})
	}
}

func handleCrawl(queue *QueueClient) fiber.Handler {
	return func(c *fiber.Ctx) error {
		var req CrawlRequest
		if err := c.BodyParser(&req); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "invalid request body",
			})
		}

		if req.URL == "" {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "url is required",
			})
		}

		resolvedIP, err := ValidateScrapeURL(req.URL)
		if err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   err.Error(),
			})
		}
		req.ResolvedIP = resolvedIP

		if req.MaxPages <= 0 {
			req.MaxPages = 10
		}

		plan, _ := c.Locals("plan").(Plan)
		if plan.MaxCrawlPages > 0 && req.MaxPages > plan.MaxCrawlPages {
			req.MaxPages = plan.MaxCrawlPages
		}
		if req.MaxPages > 1000 {
			req.MaxPages = 1000
		}

		if req.MaxDepth <= 0 {
			req.MaxDepth = 3
		}
		if req.MaxDepth > 10 {
			req.MaxDepth = 10
		}

		if req.MaxTimeout <= 0 {
			req.MaxTimeout = 300
		}
		if req.MaxTimeout > 600 {
			req.MaxTimeout = 600
		}

		if req.Format == "" {
			req.Format = "markdown"
		}
		if !validateFormat(req.Format) {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "format must be markdown, html, or text",
			})
		}

		decision := ClassifyURL(req.URL)

		if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
			return c.Status(fiber.StatusForbidden).JSON(ErrorResponse{
				Success: false,
				Error:   "this URL requires the " + string(decision.Tier) + " tier, upgrade your plan",
			})
		}

		req.ResultTTL = planResultTTL(plan)
		owner := callerOwner(c)
		jobID, err := queue.EnqueueCrawl(c.Context(), req, owner)
		if err != nil {
			log.Printf("enqueue crawl error: %v", err)
			return c.Status(fiber.StatusInternalServerError).JSON(ErrorResponse{
				Success: false,
				Error:   "failed to enqueue job",
			})
		}

		LogRouteDecision(jobID, req.URL, decision)

		return c.Status(fiber.StatusAccepted).JSON(CrawlResponse{
			Success: true,
			JobID:   jobID,
			Status:  "queued",
		})
	}
}

func handleExtract(queue *QueueClient, aiEndpoint string) fiber.Handler {
	return func(c *fiber.Ctx) error {
		if aiEndpoint == "" {
			return c.Status(fiber.StatusServiceUnavailable).JSON(ErrorResponse{
				Success: false,
				Error:   "extraction service not configured",
			})
		}

		plan, _ := c.Locals("plan").(Plan)
		if !plan.CanExtract {
			return c.Status(fiber.StatusForbidden).JSON(ErrorResponse{
				Success: false,
				Error:   "extract is not available on your current plan",
			})
		}

		var req ExtractRequest
		if err := c.BodyParser(&req); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "invalid request body",
			})
		}

		if req.URL == "" {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "url is required",
			})
		}

		resolvedIP, err := ValidateScrapeURL(req.URL)
		if err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   err.Error(),
			})
		}

		if len(req.ExtractSchema) == 0 {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "extract_schema is required",
			})
		}

		if !json.Valid(req.ExtractSchema) {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "extract_schema must be valid JSON",
			})
		}

		decision := ClassifyURL(req.URL)

		if !IsTierAllowed(decision.Tier, plan.AllowedTiers) {
			return c.Status(fiber.StatusForbidden).JSON(ErrorResponse{
				Success: false,
				Error:   "this URL requires the " + string(decision.Tier) + " tier, upgrade your plan",
			})
		}

		scrapeReq := ScrapeRequest{
			URL:           req.URL,
			Format:        "markdown",
			ExtractSchema: (*json.RawMessage)(&req.ExtractSchema),
			WaitFor:       req.WaitFor,
			Timeout:       req.Timeout,
			Wait:          req.Wait,
			ResolvedIP:    resolvedIP,
			Strategy:      decision.Strategy,
		}

		owner := callerOwner(c)
		stream := StreamForTier(decision.Tier)
		jobID, err := queue.EnqueueScrape(c.Context(), scrapeReq, owner, stream)
		if err != nil {
			log.Printf("enqueue extract error: %v", err)
			return c.Status(fiber.StatusInternalServerError).JSON(ErrorResponse{
				Success: false,
				Error:   "failed to enqueue job",
			})
		}

		LogRouteDecision(jobID, req.URL, decision)

		return c.Status(fiber.StatusAccepted).JSON(EnqueueResponse{
			Success: true,
			JobID:   jobID,
		})
	}
}

func handleCrawlStatus(queue *QueueClient) fiber.Handler {
	return func(c *fiber.Ctx) error {
		jobID := c.Params("id")
		if jobID == "" {
			return c.Status(fiber.StatusBadRequest).JSON(ErrorResponse{
				Success: false,
				Error:   "job id is required",
			})
		}

		status, err := queue.GetJobStatus(c.Context(), jobID)
		if err != nil {
			if errors.Is(err, ErrJobNotFound) {
				return c.Status(fiber.StatusNotFound).JSON(ErrorResponse{
					Success: false,
					Error:   "job not found",
				})
			}
			log.Printf("get job status error: %v", err)
			return c.Status(fiber.StatusInternalServerError).JSON(ErrorResponse{
				Success: false,
				Error:   "failed to get job status",
			})
		}

		caller := callerOwner(c)
		if subtle.ConstantTimeCompare([]byte(status.Owner), []byte(caller)) != 1 {
			return c.Status(fiber.StatusNotFound).JSON(ErrorResponse{
				Success: false,
				Error:   "job not found",
			})
		}

		status.Owner = ""
		return c.JSON(status)
	}
}
