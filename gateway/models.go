package main

import (
	"encoding/json"
	"time"
)

type ScrapeRequest struct {
	URL           string           `json:"url" validate:"required,url"`
	Format        string           `json:"format,omitempty"`
	ExtractSchema *json.RawMessage `json:"extract_schema,omitempty"`
	WaitFor       string           `json:"wait_for,omitempty"`
	Timeout       int              `json:"timeout,omitempty"`
	Wait          bool             `json:"wait,omitempty"`
	ResolvedIP    string           `json:"-"`
	Strategy      string           `json:"-"`
	// VideoID and Language are set only for transcript jobs and travel as their
	// own stream fields.
	VideoID  string `json:"-"`
	Language string `json:"-"`
}

type ScrapeResponse struct {
	Success bool       `json:"success"`
	Data    ScrapeData `json:"data,omitempty"`
}

type ScrapeData struct {
	Content       string          `json:"content"`
	Markdown      string          `json:"markdown,omitempty"`
	Metadata      PageMetadata    `json:"metadata"`
	ExtractedData json.RawMessage `json:"extracted_data,omitempty"`
}

type PageMetadata struct {
	Title       string `json:"title"`
	Description string `json:"description,omitempty"`
	Language    string `json:"language,omitempty"`
	StatusCode  int    `json:"status_code"`
	// Strategy is the extractor that produced the result ("generic" for a plain fetch).
	Strategy string `json:"strategy,omitempty"`
	// FallbackFrom names a strategy whose extractor failed before the generic fetch produced this result.
	FallbackFrom string `json:"fallback_from,omitempty"`
	// Transcript describes how a YouTube transcript was obtained (strategy
	// youtube_transcript only).
	Transcript *TranscriptMeta `json:"transcript,omitempty"`
}

// TranscriptMeta says which caption track a transcript came from, so a caller
// can tell a creator's captions from auto-generated or machine-translated ones.
type TranscriptMeta struct {
	// Kind is "manual", "auto" (YouTube's speech recognition in the original
	// language) or "translated" (a machine translation of another track).
	Kind     string `json:"kind"`
	Language string `json:"language"`
	// AvailableLanguages lists the caption languages the video offers.
	AvailableLanguages []string `json:"available_languages,omitempty"`
}

// JobError says why a job failed, for callers that need more than status
// "failed". Workers write it next to status "failed"; the code comes from a
// fixed vocabulary and the message is written by the worker, never taken from
// third-party text. The transcript worker uses: no_captions,
// no_track_in_language, rate_limited, blocked, unavailable, timeout, internal.
type JobError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type CrawlRequest struct {
	URL             string        `json:"url" validate:"required,url"`
	MaxPages        int           `json:"max_pages,omitempty"`
	MaxDepth        int           `json:"max_depth,omitempty"`
	MaxTimeout      int           `json:"max_timeout,omitempty"`
	IncludePatterns []string      `json:"include_patterns,omitempty"`
	ExcludePatterns []string      `json:"exclude_patterns,omitempty"`
	Format          string        `json:"format,omitempty"`
	ResolvedIP      string        `json:"-"`
	ResultTTL       time.Duration `json:"result_ttl_ns,omitempty"`
}

type CrawlResponse struct {
	Success bool   `json:"success"`
	JobID   string `json:"job_id"`
	Status  string `json:"status"`
}

type JobStatus struct {
	JobID          string           `json:"job_id"`
	Status         string           `json:"status"`
	Owner          string           `json:"owner,omitempty"`
	PagesTotal     int              `json:"pages_total"`
	PagesCompleted int              `json:"pages_completed"`
	Results        []ScrapeResponse `json:"results,omitempty"`
	// Error is set with status "failed" when the worker knows why.
	Error *JobError `json:"error,omitempty"`
}

type ExtractRequest struct {
	URL           string          `json:"url" validate:"required,url"`
	ExtractSchema json.RawMessage `json:"extract_schema" validate:"required"`
	WaitFor       string          `json:"wait_for,omitempty"`
	Timeout       int             `json:"timeout,omitempty"`
	Wait          bool            `json:"wait,omitempty"`
}

type ErrorResponse struct {
	Success bool   `json:"success"`
	Error   string `json:"error"`
}

type EnqueueResponse struct {
	Success bool   `json:"success"`
	JobID   string `json:"job_id"`
}
