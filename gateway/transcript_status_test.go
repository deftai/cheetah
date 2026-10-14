package main

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestAnyFreshConsumer(t *testing.T) {
	const max = 30 * time.Second
	c := func(idle time.Duration) redis.XInfoConsumer { return redis.XInfoConsumer{Name: "w", Idle: idle} }
	tests := []struct {
		name      string
		consumers []redis.XInfoConsumer
		want      bool
	}{
		{"no consumers", nil, false},
		{"one just seen", []redis.XInfoConsumer{c(0)}, true},
		{"one seen a few seconds ago", []redis.XInfoConsumer{c(5 * time.Second)}, true},
		{"just inside the limit", []redis.XInfoConsumer{c(max - time.Millisecond)}, true},
		{"exactly at the limit is stale", []redis.XInfoConsumer{c(max)}, false},
		{"stale", []redis.XInfoConsumer{c(10 * time.Minute)}, false},
		{"all stale", []redis.XInfoConsumer{c(time.Minute), c(time.Hour)}, false},
		{"one fresh among stale", []redis.XInfoConsumer{c(time.Hour), c(2 * time.Second), c(time.Minute)}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := anyFreshConsumer(tt.consumers, max); got != tt.want {
				t.Fatalf("anyFreshConsumer = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestWorkerLiveness(t *testing.T) {
	const max = 30 * time.Second
	fresh := []redis.XInfoConsumer{{Name: "w", Idle: time.Second}}
	stale := []redis.XInfoConsumer{{Name: "w", Idle: time.Hour}}
	noGroup := errors.New("NOGROUP No such consumer group 'transcript-workers' for key name 'transcript_jobs'")
	noKey := errors.New("ERR no such key")
	outage := errors.New("dial tcp 127.0.0.1:6379: connect: connection refused")

	tests := []struct {
		name      string
		consumers []redis.XInfoConsumer
		err       error
		alive     bool
		wantErr   bool
	}{
		{"fresh consumer", fresh, nil, true, false},
		{"stale consumer", stale, nil, false, false},
		{"group with no consumers", nil, nil, false, false},
		// A group or stream that does not exist means no worker ever joined: an
		// answer ("not alive"), not a failure to answer.
		{"group missing", nil, noGroup, false, false},
		{"stream missing", nil, noKey, false, false},
		// A Redis outage is not "no worker", and must not look like a live one either.
		{"redis outage", nil, outage, false, true},
		{"redis outage with leftover consumers", fresh, outage, false, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			alive, err := workerLiveness(tt.consumers, tt.err, max)
			if alive != tt.alive || (err != nil) != tt.wantErr {
				t.Fatalf("workerLiveness = (%v, %v), want alive=%v wantErr=%v", alive, err, tt.alive, tt.wantErr)
			}
			if tt.wantErr && !errors.Is(err, tt.err) {
				t.Fatalf("the underlying error must stay inspectable: %v", err)
			}
		})
	}
}

func TestTranscriptWorkerGate(t *testing.T) {
	if msg := transcriptWorkerGate(true, nil); msg != "" {
		t.Fatalf("a live worker must not be refused: %q", msg)
	}
	if msg := transcriptWorkerGate(false, nil); msg != "transcript worker is not running" {
		t.Fatalf("no worker: %q", msg)
	}
	if msg := transcriptWorkerGate(false, errors.New("boom")); msg != "failed to check the transcript worker" {
		t.Fatalf("unanswerable check: %q", msg)
	}
	// Queueing blind is the hang the gate exists to prevent, even if a stale
	// positive answer came back alongside the error.
	if msg := transcriptWorkerGate(true, errors.New("boom")); msg != "failed to check the transcript worker" {
		t.Fatalf("an error must refuse even when alive is true: %q", msg)
	}
}

func TestIsNoGroupErr(t *testing.T) {
	for _, msg := range []string{
		"ERR no such key",
		"ERR no such key or consumer group",
		"NOGROUP No such consumer group 'transcript-workers' for key name 'transcript_jobs'",
	} {
		if !isNoGroupErr(errors.New(msg)) {
			t.Errorf("%q should mean no stream or group", msg)
		}
	}
	for _, msg := range []string{"dial tcp 127.0.0.1:1: connect: connection refused", "context deadline exceeded", "WRONGTYPE Operation against a key", ""} {
		if isNoGroupErr(errors.New(msg)) {
			t.Errorf("%q must not be mistaken for a missing group (it is an outage)", msg)
		}
	}
	if isNoGroupErr(nil) {
		t.Error("nil is not an error")
	}
}

func TestLivenessCacheExpiresOnTheClockNotTheCall(t *testing.T) {
	c := livenessCache{ttl: 5 * time.Second}
	t0 := time.Unix(1_000_000, 0)

	if _, fresh := c.get(t0); fresh {
		t.Fatal("an empty cache must not report a fresh answer")
	}
	c.set(true, t0)
	if ok, fresh := c.get(t0.Add(4999 * time.Millisecond)); !fresh || !ok {
		t.Fatalf("answer should still be fresh just before the ttl: ok=%v fresh=%v", ok, fresh)
	}
	if _, fresh := c.get(t0.Add(5 * time.Second)); fresh {
		t.Fatal("answer must expire at the ttl")
	}
	// A negative answer is cached too, and replaced by a later positive one.
	c.set(false, t0.Add(10*time.Second))
	if ok, fresh := c.get(t0.Add(11 * time.Second)); !fresh || ok {
		t.Fatalf("a cached 'no worker' should be returned as such: ok=%v fresh=%v", ok, fresh)
	}
	c.set(true, t0.Add(20*time.Second))
	if ok, fresh := c.get(t0.Add(21 * time.Second)); !fresh || !ok {
		t.Fatalf("the newer answer should win: ok=%v fresh=%v", ok, fresh)
	}
}

func TestMonitoredStreamsFor(t *testing.T) {
	base := monitoredStreamsFor(false)
	want := []string{"scrape_jobs", "browser_jobs", "stealth_jobs", "crawl_jobs"}
	if len(base) != len(want) {
		t.Fatalf("base streams = %v", base)
	}
	for i, s := range base {
		if s.stream != want[i] {
			t.Fatalf("base stream %d = %q, want %q", i, s.stream, want[i])
		}
		if s.stream == transcriptStream || s.tier == tierTranscript {
			t.Fatalf("the transcript stream must not be monitored when disabled: %v", base)
		}
	}

	with := monitoredStreamsFor(true)
	if len(with) != len(want)+1 {
		t.Fatalf("with transcript = %v", with)
	}
	last := with[len(with)-1]
	if last.stream != "transcript_jobs" || last.tier != "transcript" {
		t.Fatalf("transcript stream entry = %+v", last)
	}
	// Building the enabled list must not alias or grow the shared base list.
	if again := monitoredStreamsFor(false); len(again) != len(want) {
		t.Fatalf("base list was modified: %v", again)
	}
}

func TestComputeOverallStatusWithTranscriptTier(t *testing.T) {
	up := RedisHealth{Connected: true}
	fast := WorkerHealth{ActiveConsumers: 1}
	tests := []struct {
		name    string
		redis   RedisHealth
		workers map[string]WorkerHealth
		want    string
	}{
		{"redis down", RedisHealth{}, map[string]WorkerHealth{tierFast: fast}, "unhealthy"},
		{"no fast worker", up, map[string]WorkerHealth{tierFast: {}}, "degraded"},
		{"transcript tier absent (feature off)", up, map[string]WorkerHealth{tierFast: fast}, "healthy"},
		{"transcript enabled, no consumer", up, map[string]WorkerHealth{tierFast: fast, tierTranscript: {}}, "degraded"},
		{"transcript enabled, consumer running", up, map[string]WorkerHealth{tierFast: fast, tierTranscript: {ActiveConsumers: 1}}, "healthy"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := computeOverallStatus(tt.redis, tt.workers); got != tt.want {
				t.Fatalf("computeOverallStatus = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestJobStatusCarriesAWorkerWrittenError(t *testing.T) {
	// The shape a worker writes next to status "failed".
	raw := `{"job_id":"j1","status":"failed","pages_total":1,"pages_completed":0,"results":[],"error":{"code":"no_captions","message":"this video has no captions"}}`
	var got JobStatus
	if err := json.Unmarshal([]byte(raw), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status != "failed" || got.Error == nil || got.Error.Code != "no_captions" || got.Error.Message != "this video has no captions" {
		t.Fatalf("worker failure not decoded: %+v", got)
	}
	out, _ := json.Marshal(got)
	if !strings.Contains(string(out), `"error":{"code":"no_captions","message":"this video has no captions"}`) {
		t.Fatalf("error must be returned to the caller: %s", out)
	}

	// Existing statuses are unchanged: no error key unless there is one.
	ok, _ := json.Marshal(JobStatus{JobID: "j2", Status: "completed"})
	if strings.Contains(string(ok), "error") {
		t.Fatalf("a job without an error must not gain an error key: %s", ok)
	}
}

func TestPageMetadataTranscriptRoundTrip(t *testing.T) {
	raw := `{"title":"Video","status_code":200,"strategy":"youtube_transcript","transcript":{"kind":"auto","language":"ko-orig","available_languages":["ko-orig","en"]}}`
	var md PageMetadata
	if err := json.Unmarshal([]byte(raw), &md); err != nil {
		t.Fatal(err)
	}
	if md.Transcript == nil || md.Transcript.Kind != "auto" || md.Transcript.Language != "ko-orig" || len(md.Transcript.AvailableLanguages) != 2 {
		t.Fatalf("transcript metadata not decoded: %+v", md.Transcript)
	}
	out, _ := json.Marshal(md)
	if !strings.Contains(string(out), `"transcript":{"kind":"auto","language":"ko-orig","available_languages":["ko-orig","en"]}`) {
		t.Fatalf("transcript metadata must reach the caller: %s", out)
	}
	plain, _ := json.Marshal(PageMetadata{Title: "t", StatusCode: 200})
	if strings.Contains(string(plain), "transcript") {
		t.Fatalf("non-transcript metadata must not gain a transcript key: %s", plain)
	}
}
