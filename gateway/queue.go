package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
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
	// transcriptLive caches the transcript-worker liveness answer for a few
	// seconds so each extract_transcript call does not cost Redis round trips.
	transcriptLive livenessCache
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
	} else if strings.Contains(v, "@") {
		// Credentials without a scheme (user:pw@host:6379) would be dialed as a
		// literal host, and the dial error echoes the whole string.
		return "", errors.New("not a valid redis address (credentials require a redis:// scheme)")
	}
	return v, nil
}

func NewQueueClient(addr string) *QueueClient {
	addr = strings.TrimSpace(addr)
	opts, err := redis.ParseURL(addr)
	if err != nil {
		opts = &redis.Options{Addr: addr}
	}
	// Without this go-redis ignores a context's deadline while waiting for a
	// reply and uses only its own read timeout (and retries), so a Redis that
	// accepts a command and stalls would hold a caller far past the deadline
	// it set (the liveness check's one second, the health endpoint's three).
	opts.ContextTimeoutEnabled = true
	rdb := redis.NewClient(opts)
	return &QueueClient{rdb: rdb, transcriptLive: livenessCache{ttl: transcriptLivenessTTL}}
}

const (
	// transcriptGroup is the consumer group the transcript worker joins.
	transcriptGroup = "transcript-workers"
	// transcriptWorkerMaxIdle is how long a consumer may go without touching the
	// stream and still count as running. This is a REQUIREMENT on the worker, not
	// something the gateway can make true: it must keep calling XREADGROUP with a
	// short BLOCK (5 s at most) on a thread that is not busy running a job, so
	// its idle time stays well below this even while jobs run. A worker that
	// blocks on a long job, or uses BLOCK 0, is reported as not running.
	transcriptWorkerMaxIdle = 30 * time.Second
	transcriptLivenessTTL   = livenessCacheTTL
	// transcriptLivenessTimeout bounds the Redis call, so a slow Redis cannot
	// hold a request open (and a slow answer is an error, not "no worker").
	transcriptLivenessTimeout = time.Second
)

// countFreshConsumers is the number of consumers seen within maxIdle. The
// gateway's liveness gate and /v1/health both use it, so they cannot disagree
// about whether a transcript worker is present.
func countFreshConsumers(consumers []redis.XInfoConsumer, maxIdle time.Duration) int {
	n := 0
	for _, c := range consumers {
		if c.Idle < maxIdle {
			n++
		}
	}
	return n
}

// anyFreshConsumer reports whether some consumer has been seen within maxIdle.
func anyFreshConsumer(consumers []redis.XInfoConsumer, maxIdle time.Duration) bool {
	return countFreshConsumers(consumers, maxIdle) > 0
}

// isNoGroupErr matches Redis's answer for a stream or consumer group that does
// not exist, which for liveness means "no worker has ever joined".
func isNoGroupErr(err error) bool {
	if err == nil {
		return false
	}
	return isNoStreamErr(err) || strings.HasPrefix(err.Error(), "NOGROUP")
}

// livenessCache holds one boolean for ttl. The clock is a parameter so the
// expiry logic is testable without sleeping.
type livenessCache struct {
	mu  sync.Mutex
	ttl time.Duration
	ok  bool
	at  time.Time
}

func (c *livenessCache) get(now time.Time) (ok, fresh bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.at.IsZero() || now.Sub(c.at) >= c.ttl {
		return false, false
	}
	return c.ok, true
}

func (c *livenessCache) set(ok bool, now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ok, c.at = ok, now
}

// workerLiveness turns Redis's answer to XINFO CONSUMERS into a verdict. A
// stream or group that does not exist means no worker has ever joined (not
// alive, no error). Any other error means Redis could not answer, which is not
// the same as "no worker": it is returned as an error and must not be cached.
func workerLiveness(consumers []redis.XInfoConsumer, err error, maxIdle time.Duration) (bool, error) {
	if err != nil {
		if isNoGroupErr(err) {
			return false, nil
		}
		return false, fmt.Errorf("transcript worker liveness: %w", err)
	}
	return anyFreshConsumer(consumers, maxIdle), nil
}

// freshTranscriptConsumers counts the transcript group's consumers seen
// recently, for /v1/health. It applies the same test as the liveness gate.
func (q *QueueClient) freshTranscriptConsumers(ctx context.Context) (int, error) {
	consumers, err := q.rdb.XInfoConsumers(ctx, transcriptStream, transcriptGroup).Result()
	if err != nil {
		if isNoGroupErr(err) {
			return 0, nil
		}
		return 0, err
	}
	return countFreshConsumers(consumers, transcriptWorkerMaxIdle), nil
}

// TranscriptWorkerAlive reports whether a transcript worker is consuming the
// transcript stream: the consumer group exists and one of its consumers was
// seen recently.
func (q *QueueClient) TranscriptWorkerAlive(ctx context.Context) (bool, error) {
	if ok, fresh := q.transcriptLive.get(time.Now()); fresh {
		return ok, nil
	}
	ctx, cancel := context.WithTimeout(ctx, transcriptLivenessTimeout)
	defer cancel()
	consumers, err := q.rdb.XInfoConsumers(ctx, transcriptStream, transcriptGroup).Result()
	ok, lerr := workerLiveness(consumers, err, transcriptWorkerMaxIdle)
	if lerr != nil {
		return false, lerr
	}
	q.transcriptLive.set(ok, time.Now())
	return ok, nil
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

	// The queued status and owner are written BEFORE the stream entry. A worker can
	// finish a job within a few Redis round trips (the transcript worker answers a
	// cached video without any network fetch), and a "queued" written after the
	// worker's result would overwrite it, losing the result for the whole status TTL.
	pipe := q.rdb.Pipeline()
	pipe.Set(ctx, jobKey(jobID), mustMarshal(JobStatus{
		JobID:  jobID,
		Status: "queued",
	}), jobTTL)
	pipe.Set(ctx, ownerKey(jobID), owner, jobTTL)
	if _, err = pipe.Exec(ctx); err != nil {
		return "", fmt.Errorf("set initial job status: %w", err)
	}

	err = q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: targetStream,
		MaxLen: streamMaxLen,
		Approx: true,
		Values: scrapeStreamValues(jobID, req, string(payload)),
	}).Err()
	if err != nil {
		// Nothing will ever process this job: do not leave a status that reads "queued".
		// Best effort on a fresh context: the caller's may be the reason XADD failed.
		cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		q.rdb.Del(cleanup, jobKey(jobID), ownerKey(jobID))
		return "", fmt.Errorf("enqueue scrape job: %w", err)
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
