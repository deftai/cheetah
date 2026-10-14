package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/html"
)

const (
	crawlMaxBodyBytes = 10 * 1024 * 1024
	crawlConcurrency  = 5
	crawlUserAgent    = "Mozilla/5.0 (compatible; Cheetah/1.0; +https://cheetah.dev/bot)"
	crawlMaxTotalSize = 100 * 1024 * 1024
)

type crawlResult struct {
	URL        string `json:"url"`
	StatusCode int    `json:"status_code"`
	Title      string `json:"title,omitempty"`
	Content    string `json:"content,omitempty"`
	Depth      int    `json:"depth"`
	Error      string `json:"error,omitempty"`
}

type CrawlManager struct {
	httpClient *http.Client
	queue      *QueueClient
}

func newCrawlHTTPClient() *http.Client {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			ips, err := net.DefaultResolver.LookupHost(ctx, host)
			if err != nil {
				return nil, err
			}
			for _, ipStr := range ips {
				ip := net.ParseIP(ipStr)
				if ip == nil || isPrivateIP(ip) {
					return nil, fmt.Errorf("connection to private or unparseable IP blocked")
				}
			}
			if len(ips) == 0 {
				return nil, fmt.Errorf("no addresses found for %s", host)
			}
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, net.JoinHostPort(ips[0], port))
		},
	}

	return &http.Client{
		Timeout:   30 * time.Second,
		Transport: transport,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 {
				return fmt.Errorf("too many redirects")
			}
			_, err := ValidateScrapeURL(req.URL.String())
			if err != nil {
				return fmt.Errorf("redirect to private address blocked")
			}
			return nil
		},
	}
}

func NewCrawlManager(queue *QueueClient) *CrawlManager {
	return &CrawlManager{
		httpClient: newCrawlHTTPClient(),
		queue:      queue,
	}
}

func (cm *CrawlManager) RunCrawl(ctx context.Context, jobID string, req CrawlRequest) {
	timeout := time.Duration(req.MaxTimeout) * time.Second
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	seedURL, err := url.Parse(req.URL)
	if err != nil {
		cm.failJob(jobID, "invalid seed URL", req.ResultTTL)
		return
	}

	cm.updateJobStatus(jobID, "running", 0, req.ResultTTL)

	visited := make(map[string]bool)
	visited[normalizeURL(req.URL)] = true

	type queueItem struct {
		url   string
		depth int
	}
	queue := []queueItem{{url: req.URL, depth: 0}}

	var results []crawlResult
	pagesDone := 0
	totalBytes := 0

	for len(queue) > 0 && pagesDone < req.MaxPages {
		if ctx.Err() != nil {
			break
		}

		batchSize := crawlConcurrency
		if batchSize > len(queue) {
			batchSize = len(queue)
		}
		if batchSize > req.MaxPages-pagesDone {
			batchSize = req.MaxPages - pagesDone
		}

		batch := queue[:batchSize]
		queue = queue[batchSize:]

		var wg sync.WaitGroup
		batchResults := make([]crawlResult, batchSize)

		for i, item := range batch {
			if ctx.Err() != nil {
				break
			}
			wg.Add(1)
			go func(idx int, it queueItem) {
				defer wg.Done()
				r := cm.fetchPage(ctx, it.url)
				r.Depth = it.depth
				batchResults[idx] = r
			}(i, item)
		}
		wg.Wait()

		for _, r := range batchResults {
			if r.URL == "" {
				continue
			}
			pagesDone++
			totalBytes += len(r.Content)

			if totalBytes > crawlMaxTotalSize {
				r.Content = ""
				r.Error = "total crawl size limit exceeded"
			}

			if r.Depth < req.MaxDepth && r.Error == "" && r.Content != "" {
				links := extractLinks(r.Content, r.URL)
				links = filterLinks(links, seedURL, req.IncludePatterns, req.ExcludePatterns)
				for _, link := range links {
					norm := normalizeURL(link)
					if !visited[norm] {
						visited[norm] = true
						queue = append(queue, queueItem{url: link, depth: r.Depth + 1})
					}
				}
			}

			results = append(results, r)

			if totalBytes > crawlMaxTotalSize {
				break
			}
		}

		cm.updateJobStatus(jobID, "running", pagesDone, req.ResultTTL)

		if totalBytes > crawlMaxTotalSize {
			break
		}
	}

	status := "completed"
	if ctx.Err() != nil {
		status = "partial"
	}

	cm.finalizeJob(jobID, status, pagesDone, results, req.ResultTTL)
}

func (cm *CrawlManager) fetchPage(ctx context.Context, rawURL string) crawlResult {
	result := crawlResult{URL: rawURL}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		result.Error = "fetch failed"
		log.Printf("crawl request setup %s: %v", rawURL, err)
		return result
	}
	httpReq.Header.Set("User-Agent", crawlUserAgent)
	httpReq.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")

	resp, err := cm.httpClient.Do(httpReq)
	if err != nil {
		result.Error = "fetch failed"
		log.Printf("crawl fetch %s: %v", rawURL, err)
		return result
	}
	defer resp.Body.Close()

	result.StatusCode = resp.StatusCode

	if resp.StatusCode >= 400 {
		result.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
		return result
	}

	ct := resp.Header.Get("Content-Type")
	if ct != "" && !strings.Contains(ct, "text/html") && !strings.Contains(ct, "application/xhtml") {
		result.Error = "non-HTML content"
		return result
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, crawlMaxBodyBytes))
	if err != nil {
		result.Error = "fetch failed"
		log.Printf("crawl body read %s: %v", rawURL, err)
		return result
	}

	result.Content = string(body)
	result.Title = extractTitle(result.Content)
	return result
}

func extractLinks(htmlContent string, baseURL string) []string {
	base, err := url.Parse(baseURL)
	if err != nil {
		return nil
	}

	tokenizer := html.NewTokenizer(strings.NewReader(htmlContent))
	var links []string
	seen := make(map[string]bool)

	for {
		tt := tokenizer.Next()
		if tt == html.ErrorToken {
			break
		}
		if tt != html.StartTagToken && tt != html.SelfClosingTagToken {
			continue
		}

		t := tokenizer.Token()
		if t.Data != "a" {
			continue
		}

		for _, attr := range t.Attr {
			if attr.Key != "href" {
				continue
			}
			href := strings.TrimSpace(attr.Val)
			if href == "" || strings.HasPrefix(href, "#") || strings.HasPrefix(href, "javascript:") || strings.HasPrefix(href, "mailto:") || strings.HasPrefix(href, "tel:") {
				continue
			}

			resolved, err := base.Parse(href)
			if err != nil {
				continue
			}

			resolved.Fragment = ""
			link := resolved.String()

			if !seen[link] {
				seen[link] = true
				links = append(links, link)
			}
		}
	}

	return links
}

func filterLinks(links []string, seedURL *url.URL, includePatterns, excludePatterns []string) []string {
	var filtered []string
	for _, link := range links {
		parsed, err := url.Parse(link)
		if err != nil {
			continue
		}

		if parsed.Scheme != "http" && parsed.Scheme != "https" {
			continue
		}

		if parsed.Hostname() != seedURL.Hostname() {
			continue
		}

		if skipExtensions.MatchString(parsed.Path) {
			continue
		}

		if len(includePatterns) > 0 && !matchesAny(link, includePatterns) {
			continue
		}
		if len(excludePatterns) > 0 && matchesAny(link, excludePatterns) {
			continue
		}

		filtered = append(filtered, link)
	}
	return filtered
}

var skipExtensions = regexp.MustCompile(`(?i)\.(pdf|zip|tar|gz|exe|dmg|pkg|deb|rpm|iso|img|png|jpg|jpeg|gif|svg|webp|ico|bmp|tiff|mp3|mp4|avi|mov|wmv|flv|webm|ogg|wav|css|js|woff|woff2|ttf|eot|otf|map)$`)

func matchesAny(s string, patterns []string) bool {
	for _, p := range patterns {
		if strings.Contains(s, p) {
			return true
		}
	}
	return false
}

func normalizeURL(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	parsed.Fragment = ""
	return strings.TrimRight(parsed.String(), "/")
}

func extractTitle(htmlContent string) string {
	tokenizer := html.NewTokenizer(strings.NewReader(htmlContent))
	inTitle := false
	for {
		tt := tokenizer.Next()
		if tt == html.ErrorToken {
			return ""
		}
		if tt == html.StartTagToken {
			t := tokenizer.Token()
			if t.Data == "title" {
				inTitle = true
			}
		} else if tt == html.TextToken && inTitle {
			return strings.TrimSpace(tokenizer.Token().Data)
		} else if tt == html.EndTagToken {
			t := tokenizer.Token()
			if t.Data == "title" {
				inTitle = false
			}
		}
	}
}

// writeJob stores the job status and refreshes the owner key so the two
// expire together and results are never orphaned from their owner.
func (cm *CrawlManager) writeJob(jobID string, js JobStatus, ttl time.Duration) {
	ctx := context.Background()
	ttl = effectiveTTL(ttl)
	cm.queue.rdb.Set(ctx, jobKey(jobID), mustMarshal(js), ttl)
	cm.queue.rdb.Expire(ctx, ownerKey(jobID), ttl)
}

func (cm *CrawlManager) updateJobStatus(jobID string, status string, pagesCompleted int, ttl time.Duration) {
	jobStatus := JobStatus{
		JobID:          jobID,
		Status:         status,
		PagesCompleted: pagesCompleted,
	}
	cm.writeJob(jobID, jobStatus, ttl)
}

func (cm *CrawlManager) failJob(jobID string, reason string, ttl time.Duration) {
	cm.updateJobStatus(jobID, "failed", 0, ttl)
	log.Printf("crawl %s failed: %s", jobID, reason)
}

func (cm *CrawlManager) finalizeJob(jobID string, status string, pagesTotal int, results []crawlResult, ttl time.Duration) {
	scrapeResults := make([]ScrapeResponse, 0, len(results))
	for _, r := range results {
		if r.Error != "" {
			continue
		}
		scrapeResults = append(scrapeResults, ScrapeResponse{
			Success: true,
			Data: ScrapeData{
				Content: r.Content,
				Metadata: PageMetadata{
					Title:      r.Title,
					StatusCode: r.StatusCode,
				},
			},
		})
	}

	jobStatus := JobStatus{
		JobID:          jobID,
		Status:         status,
		PagesTotal:     pagesTotal,
		PagesCompleted: pagesTotal,
		Results:        scrapeResults,
	}
	cm.writeJob(jobID, jobStatus, ttl)
}
