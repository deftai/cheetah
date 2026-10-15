package main

import (
	"context"
	"errors"
	"net"
	"strings"
	"sync"
	"testing"

	"github.com/redis/go-redis/v9"
)

// recorder is a go-redis hook that records every command in order and answers it
// itself, so no Redis is needed. failXAdd makes XADD return an error.
type recorder struct {
	mu      sync.Mutex
	cmds    []string
	failAdd bool
}

func (r *recorder) DialHook(next redis.DialHook) redis.DialHook {
	return func(ctx context.Context, network, addr string) (net.Conn, error) { return nil, errors.New("no network in this test") }
}

func (r *recorder) note(c redis.Cmder) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	name := strings.ToLower(c.Name())
	r.cmds = append(r.cmds, name)
	if name == "xadd" && r.failAdd {
		return errors.New("xadd refused")
	}
	return nil
}

func (r *recorder) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error { return r.note(cmd) }
}

func (r *recorder) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		for _, c := range cmds {
			if err := r.note(c); err != nil {
				return err
			}
		}
		return nil
	}
}

func recordingQueue(failAdd bool) (*QueueClient, *recorder) {
	q := NewQueueClient("localhost:6379")
	rec := &recorder{failAdd: failAdd}
	q.rdb.AddHook(rec)
	return q, rec
}

// A worker can answer within a few Redis round trips (a cached transcript), so the
// queued status must exist before the stream entry does: a "queued" written after
// the worker's result would overwrite it.
func TestEnqueueScrapeWritesStatusBeforeTheStreamEntry(t *testing.T) {
	q, rec := recordingQueue(false)
	if _, err := q.EnqueueScrape(context.Background(), ScrapeRequest{URL: "https://example.com/"}, "owner", "scrape_jobs"); err != nil {
		t.Fatal(err)
	}
	got := strings.Join(rec.cmds, ",")
	if got != "set,set,xadd" {
		t.Fatalf("commands in order = %s, want set,set,xadd", got)
	}
}

func TestEnqueueScrapeRemovesTheStatusWhenTheStreamWriteFails(t *testing.T) {
	q, rec := recordingQueue(true)
	if _, err := q.EnqueueScrape(context.Background(), ScrapeRequest{URL: "https://example.com/"}, "owner", "scrape_jobs"); err == nil {
		t.Fatal("expected an error")
	}
	got := strings.Join(rec.cmds, ",")
	if got != "set,set,xadd,del" {
		t.Fatalf("commands in order = %s, want set,set,xadd,del (a job nothing will process must not read as queued)", got)
	}
}
