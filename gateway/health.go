package main

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/gofiber/fiber/v2"
	"github.com/redis/go-redis/v9"
)

const (
	healthTimeout    = 3 * time.Second
	livenessCacheTTL = 5 * time.Second
	tierFast         = "fast"
	tierTranscript   = "transcript"
)

type monitoredStream struct {
	stream string
	tier   string
}

var baseMonitoredStreams = []monitoredStream{
	{"scrape_jobs", tierFast},
	{"browser_jobs", "browser"},
	{"stealth_jobs", "stealth"},
	{"crawl_jobs", "crawl"},
}

// monitoredStreamsFor adds the transcript stream only when the transcript
// worker is enabled, so deployments without it do not report a worker tier
// that can never exist.
func monitoredStreamsFor(transcriptEnabled bool) []monitoredStream {
	if !transcriptEnabled {
		return baseMonitoredStreams
	}
	return append(append([]monitoredStream{}, baseMonitoredStreams...), monitoredStream{transcriptStream, tierTranscript})
}

type HealthResponse struct {
	Status  string                  `json:"status"`
	Redis   RedisHealth             `json:"redis"`
	Queues  map[string]QueueHealth  `json:"queues,omitempty"`
	Workers map[string]WorkerHealth `json:"workers,omitempty"`
}

type RedisHealth struct {
	Connected bool   `json:"connected"`
	LatencyMs *int64 `json:"latency_ms,omitempty"`
	Error     string `json:"error,omitempty"`
}

type QueueHealth struct {
	Length          int64  `json:"length"`
	Consumers       int    `json:"consumers"`
	OldestPendingMs *int64 `json:"oldest_pending_ms,omitempty"`
}

type WorkerHealth struct {
	ActiveConsumers int    `json:"active_consumers"`
	IdleMs          *int64 `json:"idle_ms,omitempty"`
}

type livenessEntry struct {
	status string
	code   int
	at     time.Time
}

func (q *QueueClient) PingLatency(ctx context.Context) (time.Duration, error) {
	start := time.Now()
	err := q.rdb.Ping(ctx).Err()
	return time.Since(start), err
}

type streamSnapshot struct {
	length int64
	groups []redis.XInfoGroup
	err    error
}

func (q *QueueClient) streamInfoBatch(ctx context.Context, streams []monitoredStream) map[string]streamSnapshot {
	out := make(map[string]streamSnapshot, len(streams))

	pipe := q.rdb.Pipeline()
	xlenCmds := make(map[string]*redis.IntCmd, len(streams))
	xinfoCmds := make(map[string]*redis.XInfoGroupsCmd, len(streams))
	for _, s := range streams {
		xlenCmds[s.stream] = pipe.XLen(ctx, s.stream)
		xinfoCmds[s.stream] = pipe.XInfoGroups(ctx, s.stream)
	}
	_, _ = pipe.Exec(ctx)

	for _, s := range streams {
		snap := streamSnapshot{}
		snap.length, snap.err = xlenCmds[s.stream].Result()
		if snap.err != nil {
			out[s.stream] = snap
			continue
		}
		groups, err := xinfoCmds[s.stream].Result()
		if err != nil {
			if !isNoStreamErr(err) {
				snap.err = err
			}
		} else {
			snap.groups = groups
		}
		out[s.stream] = snap
	}

	return out
}

func (q *QueueClient) OldestPending(ctx context.Context, stream, group string) (*int64, error) {
	pending, err := q.rdb.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: stream,
		Group:  group,
		Start:  "-",
		End:    "+",
		Count:  1,
	}).Result()
	if err != nil || len(pending) == 0 {
		return nil, err
	}
	ms := pending[0].Idle.Milliseconds()
	return &ms, nil
}

func isNoStreamErr(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return msg == "ERR no such key" || msg == "ERR no such key or consumer group"
}

func handleHealthLiveness(queue *QueueClient) fiber.Handler {
	var (
		mu    sync.Mutex
		cache livenessEntry
	)

	return func(c *fiber.Ctx) error {
		mu.Lock()
		if time.Since(cache.at) < livenessCacheTTL {
			status, code := cache.status, cache.code
			mu.Unlock()
			return c.Status(code).JSON(fiber.Map{"status": status})
		}
		mu.Unlock()

		ctx, cancel := context.WithTimeout(context.Background(), healthTimeout)
		defer cancel()

		_, pingErr := queue.PingLatency(ctx)
		status := "healthy"
		code := fiber.StatusOK
		if pingErr != nil {
			status = "unhealthy"
			code = fiber.StatusServiceUnavailable
		}

		mu.Lock()
		cache = livenessEntry{status: status, code: code, at: time.Now()}
		mu.Unlock()

		return c.Status(code).JSON(fiber.Map{"status": status})
	}
}

func handleHealthDiagnostic(queue *QueueClient, transcriptEnabled bool) fiber.Handler {
	streams := monitoredStreamsFor(transcriptEnabled)
	return func(c *fiber.Ctx) error {
		ctx, cancel := context.WithTimeout(context.Background(), healthTimeout)
		defer cancel()

		latency, pingErr := queue.PingLatency(ctx)
		redisHealth := RedisHealth{Connected: pingErr == nil}
		if pingErr == nil {
			ms := latency.Milliseconds()
			redisHealth.LatencyMs = &ms
		} else {
			redisHealth.Error = "redis connection failed"
			log.Printf("health check: redis ping failed: %v", pingErr)
		}

		queues := make(map[string]QueueHealth)
		workers := make(map[string]WorkerHealth)

		if pingErr == nil {
			snapshots := queue.streamInfoBatch(ctx, streams)

			for _, s := range streams {
				snap := snapshots[s.stream]
				qh := QueueHealth{Length: snap.length}
				wh := WorkerHealth{}

				if snap.err == nil {
					for _, g := range snap.groups {
						n := int(g.Consumers)
						qh.Consumers += n
						wh.ActiveConsumers += n

						oldest, pendErr := queue.OldestPending(ctx, s.stream, g.Name)
						if pendErr == nil && oldest != nil {
							if qh.OldestPendingMs == nil || *oldest > *qh.OldestPendingMs {
								qh.OldestPendingMs = oldest
							}
						}
					}

					if wh.ActiveConsumers > 0 {
						wh.IdleMs = queue.minConsumerIdle(ctx, s.stream, snap.groups)
					}
				}

				queues[s.stream] = qh
				workers[s.tier] = wh
			}
		}

		overall := computeOverallStatus(redisHealth, workers)

		resp := HealthResponse{
			Status:  overall,
			Redis:   redisHealth,
			Queues:  queues,
			Workers: workers,
		}

		code := fiber.StatusOK
		if overall == "unhealthy" {
			code = fiber.StatusServiceUnavailable
		}

		return c.Status(code).JSON(resp)
	}
}

func (q *QueueClient) minConsumerIdle(ctx context.Context, stream string, groups []redis.XInfoGroup) *int64 {
	var globalMin int64 = -1
	for _, g := range groups {
		consumers, err := q.rdb.XInfoConsumers(ctx, stream, g.Name).Result()
		if err != nil {
			continue
		}
		for _, con := range consumers {
			idle := con.Idle.Milliseconds()
			if globalMin < 0 || idle < globalMin {
				globalMin = idle
			}
		}
	}
	if globalMin >= 0 {
		return &globalMin
	}
	return nil
}

func computeOverallStatus(r RedisHealth, workers map[string]WorkerHealth) string {
	if !r.Connected {
		return "unhealthy"
	}

	fastWorker, hasFast := workers[tierFast]
	if !hasFast || fastWorker.ActiveConsumers == 0 {
		return "degraded"
	}

	// The transcript tier is only reported when the operator enabled it, so a
	// missing worker there is a fault, not an absent optional feature.
	if tw, ok := workers[tierTranscript]; ok && tw.ActiveConsumers == 0 {
		return "degraded"
	}

	return "healthy"
}
