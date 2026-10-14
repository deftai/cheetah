package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

var ErrJobNotFound = errors.New("job not found")

const jobKeyPrefix = "job_results"
const jobTTL = 1 * time.Hour
const streamMaxLen int64 = 10000

func jobKey(jobID string) string {
	return fmt.Sprintf("%s:%s", jobKeyPrefix, jobID)
}

func ownerKey(jobID string) string {
	return fmt.Sprintf("job_owner:%s", jobID)
}

type QueueClient struct {
	rdb *redis.Client
}

// checkRedisURL trims stray whitespace from raw and, when it looks like a URL
// (contains "://"), requires that it parses. A bare "host:port" is returned
// as-is. The error never carries the value or the parser's message: both echo
// the URL, including its password.
func checkRedisURL(raw string) (string, error) {
	v := strings.TrimSpace(raw)
	if strings.Contains(v, "://") {
		if _, err := redis.ParseURL(v); err != nil {
			return "", errors.New("not a valid redis URL (check for stray characters or an unescaped character in the password)")
		}
	}
	return v, nil
}

func NewQueueClient(addr string) *QueueClient {
	addr = strings.TrimSpace(addr)
	opts, err := redis.ParseURL(addr)
	if err != nil {
		opts = &redis.Options{Addr: addr}
	}
	rdb := redis.NewClient(opts)
	return &QueueClient{rdb: rdb}
}

func (q *QueueClient) Ping(ctx context.Context) error {
	return q.rdb.Ping(ctx).Err()
}

func (q *QueueClient) Close() error {
	return q.rdb.Close()
}

func effectiveTTL(requested time.Duration) time.Duration {
	if requested <= 0 {
		return jobTTL
	}
	if requested > maxResultRetention {
		return maxResultRetention
	}
	return requested
}

// scrapeStreamValues builds the stream entry for a scrape-style job. The
// transcript fields are added only when set, so entries for every other stream
// keep exactly the shape they had.
func scrapeStreamValues(jobID string, req ScrapeRequest, payload string) map[string]interface{} {
	values := map[string]interface{}{
		"job_id":      jobID,
		"url":         req.URL,
		"format":      req.Format,
		"resolved_ip": req.ResolvedIP,
		"strategy":    req.Strategy,
		"payload":     payload,
	}
	if req.VideoID != "" {
		values["video_id"] = req.VideoID
	}
	if req.Language != "" {
		values["language"] = req.Language
	}
	return values
}

func (q *QueueClient) EnqueueScrape(ctx context.Context, req ScrapeRequest, owner string, stream ...string) (string, error) {
	jobID := uuid.New().String()

	payload, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal scrape request: %w", err)
	}

	targetStream := "scrape_jobs"
	if len(stream) > 0 && stream[0] != "" {
		targetStream = stream[0]
	}

	err = q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: targetStream,
		MaxLen: streamMaxLen,
		Approx: true,
		Values: scrapeStreamValues(jobID, req, string(payload)),
	}).Err()
	if err != nil {
		return "", fmt.Errorf("enqueue scrape job: %w", err)
	}

	pipe := q.rdb.Pipeline()
	pipe.Set(ctx, jobKey(jobID), mustMarshal(JobStatus{
		JobID:  jobID,
		Status: "queued",
	}), jobTTL)
	pipe.Set(ctx, ownerKey(jobID), owner, jobTTL)
	if _, err = pipe.Exec(ctx); err != nil {
		return "", fmt.Errorf("set initial job status: %w", err)
	}

	return jobID, nil
}

func (q *QueueClient) EnqueueCrawl(ctx context.Context, req CrawlRequest, owner string) (string, error) {
	jobID := uuid.New().String()

	payload, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal crawl request: %w", err)
	}

	err = q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: "crawl_jobs",
		MaxLen: streamMaxLen,
		Approx: true,
		Values: map[string]interface{}{
			"job_id":      jobID,
			"url":         req.URL,
			"resolved_ip": req.ResolvedIP,
			"max_pages":   req.MaxPages,
			"max_depth":   req.MaxDepth,
			"max_timeout": req.MaxTimeout,
			"payload":     string(payload),
		},
	}).Err()
	if err != nil {
		return "", fmt.Errorf("enqueue crawl job: %w", err)
	}

	ttl := effectiveTTL(req.ResultTTL)
	pipe := q.rdb.Pipeline()
	pipe.Set(ctx, jobKey(jobID), mustMarshal(JobStatus{
		JobID:  jobID,
		Status: "queued",
	}), ttl)
	pipe.Set(ctx, ownerKey(jobID), owner, ttl)
	if _, err = pipe.Exec(ctx); err != nil {
		return "", fmt.Errorf("set initial job status: %w", err)
	}

	return jobID, nil
}

func (q *QueueClient) GetJobStatus(ctx context.Context, jobID string) (JobStatus, error) {
	val, err := q.rdb.Get(ctx, jobKey(jobID)).Result()
	if err == redis.Nil {
		return JobStatus{}, fmt.Errorf("job %s: %w", jobID, ErrJobNotFound)
	}
	if err != nil {
		return JobStatus{}, fmt.Errorf("get job status: %w", err)
	}

	var status JobStatus
	if err := json.Unmarshal([]byte(val), &status); err != nil {
		return JobStatus{}, fmt.Errorf("unmarshal job status: %w", err)
	}

	owner, err := q.rdb.Get(ctx, ownerKey(jobID)).Result()
	if err == nil {
		status.Owner = owner
	}

	return status, nil
}

func mustMarshal(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("mustMarshal: %v", err))
	}
	return string(b)
}
