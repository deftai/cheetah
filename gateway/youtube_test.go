package main

import (
	"net/http"
	"strings"
	"testing"

	"github.com/gofiber/fiber/v2"
)

const ytID = "dQw4w9WgXcQ"

func TestYouTubeVideoID(t *testing.T) {
	tests := []struct {
		url  string
		want string // video id, or "" when the URL must be refused
	}{
		// Supported forms.
		{"https://www.youtube.com/watch?v=" + ytID, ytID},
		{"https://youtube.com/watch?v=" + ytID, ytID},
		{"https://m.youtube.com/watch?v=" + ytID, ytID},
		{"https://www.youtube.com/watch?v=" + ytID + "&t=42s&list=PLabc", ytID},
		{"https://www.youtube.com/watch?feature=share&v=" + ytID, ytID},
		{"https://www.youtube.com/watch?v=" + ytID + "#t=10", ytID},
		{"https://youtu.be/" + ytID, ytID},
		{"https://youtu.be/" + ytID + "?t=30", ytID},
		{"https://www.youtube.com/shorts/" + ytID, ytID},
		{"https://www.youtube.com/embed/" + ytID, ytID},
		{"https://www.youtube.com/live/" + ytID, ytID},
		{"https://WWW.YouTube.COM/watch?v=" + ytID, ytID},
		{"HTTPS://youtu.be/" + ytID, ytID},
		{"https://www.youtube.com:443/watch?v=" + ytID, ytID},
		{"https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"},
		{"https://youtu.be/a-b_c-d_e-f", "a-b_c-d_e-f"},
		// A percent-encoded query value is decoded like any query parameter; only the
		// resulting 11-character id is ever used.
		{"https://www.youtube.com/watch?v=dQw4w9WgXc%51", ytID},
		// Duplicate v: the first one wins, deterministically.
		{"https://www.youtube.com/watch?v=" + ytID + "&v=aaaaaaaaaaa", ytID},

		// Other sites and look-alikes.
		{"https://example.com/watch?v=" + ytID, ""},
		{"https://youtube.com.evil.example/watch?v=" + ytID, ""},
		{"https://notyoutube.com/watch?v=" + ytID, ""},
		{"https://music.youtube.com/watch?v=" + ytID, ""},
		{"https://www.youtube-nocookie.com/embed/" + ytID, ""},
		{"https://evil.example/?u=https://youtu.be/" + ytID, ""},
		{"https://youtu.be.evil.example/" + ytID, ""},
		{"https://www.youtube.com@evil.example/watch?v=" + ytID, ""},
		// Userinfo, port, scheme.
		{"https://user@www.youtube.com/watch?v=" + ytID, ""},
		{"https://www.youtube.com:8443/watch?v=" + ytID, ""},
		{"http://www.youtube.com/watch?v=" + ytID, ""},
		{"ftp://www.youtube.com/watch?v=" + ytID, ""},
		{"//www.youtube.com/watch?v=" + ytID, ""},
		// Right site, pages that are not a single video.
		{"https://www.youtube.com/", ""},
		{"https://www.youtube.com/watch", ""},
		{"https://www.youtube.com/watch?v=", ""},
		{"https://www.youtube.com/watch?list=PLabc", ""},
		{"https://www.youtube.com/playlist?list=PLabc", ""},
		{"https://www.youtube.com/@channel", ""},
		{"https://www.youtube.com/channel/UCabc", ""},
		{"https://www.youtube.com/results?search_query=" + ytID, ""},
		{"https://www.youtube.com/watch/" + ytID, ""},
		{"https://www.youtube.com/shorts", ""},
		{"https://www.youtube.com/shorts/" + ytID + "/extra", ""},
		{"https://youtu.be/", ""},
		{"https://youtu.be/" + ytID + "/extra", ""},
		// Bad ids.
		{"https://www.youtube.com/watch?v=short", ""},
		{"https://www.youtube.com/watch?v=" + ytID + "x", ""},
		{"https://www.youtube.com/watch?v=dQw4w9WgXc%2F", ""}, // decodes to a "/"
		{"https://www.youtube.com/watch?v=dQw4w9WgXc%00", ""}, // decodes to NUL
		{"https://www.youtube.com/watch?v=dQw4w9WgXc.", ""},
		{"https://www.youtube.com/watch?v=dQw4w9WgXc ", ""},
		{"https://www.youtube.com/watch?v=dQw4w9WgXc/", ""},
		{"https://youtu.be/dQw4w9WgXc%51", ""},
		{"https://youtu.be/dQw4w9WgXc.", ""},
		{"https://www.youtube.com/watch?v=%zz", ""},
		// Path tricks and malformed input.
		{"https://www.youtube.com/shorts/../" + ytID, ""},
		{"https://www.youtube.com/embed/%2e%2e", ""},
		{"https://www.youtube.com/watch?v=" + ytID + "&x=%zz", ""},
		{`https://www.youtube.com/shorts\` + ytID, ""},
		{"https://www.youtube.com#@evil.example/watch?v=" + ytID, ""},
		{" https://youtu.be/" + ytID, ""},
		{"https://youtu.be/" + ytID + " ", ""},
		{"https://youtu.be/" + ytID + "\n", ""},
		{"https://youtu.be/" + ytID + "\x00", ""},
		{"https://youtu.be/dQw4w9 WcQ", ""},
		{"not a url", ""},
		{"", ""},
	}
	for _, tt := range tests {
		t.Run(tt.url, func(t *testing.T) {
			got, ok := YouTubeVideoID(tt.url)
			if tt.want == "" {
				if ok || got != "" {
					t.Fatalf("YouTubeVideoID(%q) = (%q, %v), want refused", tt.url, got, ok)
				}
				return
			}
			if !ok || got != tt.want {
				t.Fatalf("YouTubeVideoID(%q) = (%q, %v), want %q", tt.url, got, ok, tt.want)
			}
		})
	}
}

func TestCanonicalYouTubeURLDropsEverythingButTheID(t *testing.T) {
	id, ok := YouTubeVideoID("https://youtu.be/" + ytID + "?t=30&si=tracking&list=PLabc#frag")
	if !ok {
		t.Fatal("expected the URL to be supported")
	}
	if got := canonicalYouTubeURL(id); got != "https://www.youtube.com/watch?v="+ytID {
		t.Fatalf("canonical URL = %q", got)
	}
}

func TestValidTranscriptLanguage(t *testing.T) {
	for _, ok := range []string{"en", "ko", "fil", "pt-BR", "zh-Hans", "zh-Hans-CN", "ko-orig", "en-US", "en-orig"} {
		if !validTranscriptLanguage(ok) {
			t.Errorf("%q should be a valid language code", ok)
		}
	}
	for _, bad := range []string{
		"", "e", "english", "en_US", "en-", "-en", "en--US", "en-U", "en US", "en;rm", "en/../x",
		"../../etc", "en\n", "1n", "en-%41%41", "en-orig-orig-orig-orig", strings.Repeat("a", 21), "ko-ørig",
	} {
		if validTranscriptLanguage(bad) {
			t.Errorf("%q should be refused", bad)
		}
	}
}

func TestTranscriptURLError(t *testing.T) {
	const github = "https://github.com/rockbox37/cheetah"
	const hn = "https://news.ycombinator.com/item?id=8863"
	tests := []struct {
		url  string
		want string // substring of the message, "" for accepted
	}{
		{"https://youtu.be/" + ytID, ""},
		{github, `belongs to "get_repository_content"`},
		{hn, `belongs to "get_thread"`},
		{"https://example.com/watch?v=" + ytID, `use "scrape"`},
		{"not a url", `use "scrape"`},
		// The right site but not a video page is named as unsupported, not "wrong site".
		{"https://www.youtube.com/@channel", "not supported"},
		{"https://www.youtube.com/playlist?list=PLabc", "not supported"},
		{"http://youtu.be/" + ytID, "not supported"},
		{"https://www.youtube.com/watch?v=short", "not supported"},
		{"https://user@youtu.be/" + ytID, "not supported"},
	}
	for _, tt := range tests {
		t.Run(tt.url, func(t *testing.T) {
			id, msg := transcriptURLError(tt.url)
			if tt.want == "" {
				if msg != "" || id != ytID {
					t.Fatalf("expected %s to be accepted as %s, got id=%q msg=%q", tt.url, ytID, id, msg)
				}
				return
			}
			if id != "" || !strings.Contains(msg, tt.want) {
				t.Fatalf("id=%q msg=%q should contain %q", id, msg, tt.want)
			}
			if !strings.HasPrefix(msg, "extract_transcript only accepts ") {
				t.Fatalf("message %q should name the tool and what it accepts", msg)
			}
			if strings.Contains(msg, tt.url) {
				t.Fatalf("message must not echo the submitted URL: %q", msg)
			}
		})
	}
}

func TestScrapeStreamValuesCarryTranscriptFieldsOnlyWhenSet(t *testing.T) {
	plain := scrapeStreamValues("j1", ScrapeRequest{URL: "https://example.com", Format: "markdown", Strategy: "github_repo"}, "{}")
	for _, k := range []string{"video_id", "language"} {
		if _, ok := plain[k]; ok {
			t.Fatalf("%s must be absent from non-transcript entries: %v", k, plain)
		}
	}
	for _, k := range []string{"job_id", "url", "format", "resolved_ip", "strategy", "payload"} {
		if _, ok := plain[k]; !ok {
			t.Fatalf("%s missing from the entry: %v", k, plain)
		}
	}

	req := ScrapeRequest{URL: canonicalYouTubeURL(ytID), Format: "markdown", Strategy: StrategyYouTubeTranscript, VideoID: ytID, Language: "ko-orig"}
	got := scrapeStreamValues("j2", req, "{}")
	if got["video_id"] != ytID || got["language"] != "ko-orig" || got["strategy"] != StrategyYouTubeTranscript {
		t.Fatalf("transcript fields wrong: %v", got)
	}
	onlyID := scrapeStreamValues("j3", ScrapeRequest{URL: "u", VideoID: ytID}, "{}")
	if _, ok := onlyID["language"]; ok {
		t.Fatalf("language must be absent when unset: %v", onlyID)
	}
}

func TestLoadTranscriptWorker(t *testing.T) {
	for value, want := range map[string]bool{
		"": false, "true": true, "TRUE": true, "1": true, "t": true,
		"false": false, "0": false, "yes": false, "enabled": false, " true ": true,
	} {
		t.Setenv("TRANSCRIPT_WORKER_ENABLED", value)
		if got := loadTranscriptWorker(); got != want {
			t.Errorf("TRANSCRIPT_WORKER_ENABLED=%q: got %v, want %v", value, got, want)
		}
	}
}

func transcriptApp(enabled bool) *fiber.App {
	cfg := Config{
		RedisURL:         "localhost:6379",
		APIKeys:          []string{"test-key-123"},
		Port:             "3000",
		TranscriptWorker: enabled,
	}
	return NewApp(cfg, NewQueueClient(cfg.RedisURL))
}

func toolNames(t *testing.T, app *fiber.App) []string {
	t.Helper()
	_, result := postMCP(t, app, mcpRequest("tools/list", 2, nil))
	tools := result["result"].(map[string]interface{})["tools"].([]interface{})
	var names []string
	for _, tool := range tools {
		names = append(names, tool.(map[string]interface{})["name"].(string))
	}
	return names
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func TestExtractTranscriptToolIsOfferedOnlyWhenEnabled(t *testing.T) {
	if names := toolNames(t, transcriptApp(false)); contains(names, "extract_transcript") || len(names) != 5 {
		t.Fatalf("disabled server should offer the 5 base tools only: %v", names)
	}
	if names := toolNames(t, transcriptApp(true)); !contains(names, "extract_transcript") || len(names) != 6 {
		t.Fatalf("enabled server should offer 6 tools including extract_transcript: %v", names)
	}

	// The discovery document agrees with tools/list.
	for _, enabled := range []bool{false, true} {
		req, _ := http.NewRequest(http.MethodGet, "/.well-known/mcp.json", nil)
		resp, err := transcriptApp(enabled).Test(req)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("discovery failed: %v %v", err, resp)
		}
	}
}

func callTranscript(t *testing.T, enabled bool, args map[string]interface{}) (string, bool) {
	t.Helper()
	body := mcpRequest("tools/call", 40, map[string]interface{}{"name": "extract_transcript", "arguments": args})
	_, result := postMCP(t, transcriptApp(enabled), body)
	return toolResultText(t, result)
}

func TestExtractTranscriptDisabledSaysSo(t *testing.T) {
	text, isErr := callTranscript(t, false, map[string]interface{}{"url": "https://youtu.be/" + ytID})
	if !isErr || text != "extract_transcript is not enabled on this server" {
		t.Fatalf("expected the not-enabled error, got isError=%v %q", isErr, text)
	}
}

func TestExtractTranscriptRejectsBadInput(t *testing.T) {
	cases := []struct {
		args map[string]interface{}
		want string
	}{
		{map[string]interface{}{}, "url is required"},
		{map[string]interface{}{"url": ""}, "url is required"},
		{map[string]interface{}{"url": "https://example.com/page?token=secret"}, `use "scrape"`},
		{map[string]interface{}{"url": "https://github.com/rockbox37/cheetah"}, `belongs to "get_repository_content"`},
		{map[string]interface{}{"url": "https://www.youtube.com/@channel"}, "not supported"},
		{map[string]interface{}{"url": "https://youtu.be/" + ytID, "language": "english"}, "language must be a language code"},
		{map[string]interface{}{"url": "https://youtu.be/" + ytID, "language": "en;rm -rf"}, "language must be a language code"},
	}
	for _, tc := range cases {
		text, isErr := callTranscript(t, true, tc.args)
		if !isErr || !strings.Contains(text, tc.want) {
			t.Fatalf("%v: expected an error containing %q, got isError=%v %q", tc.args, tc.want, isErr, text)
		}
		if strings.Contains(text, "secret") || strings.Contains(text, "example.com") {
			t.Fatalf("%v: error echoed the submitted URL: %q", tc.args, text)
		}
	}
}

func TestExtractTranscriptAcceptsAValidRequestPastTheChecks(t *testing.T) {
	// Past the pure checks the call needs DNS and a queue, so here it may fail at
	// either, but never with one of this tool's own refusal messages.
	text, _ := callTranscript(t, true, map[string]interface{}{"url": "https://youtu.be/" + ytID + "?t=5", "language": "ko-orig"})
	for _, refusal := range []string{"only accepts", "not enabled", "language must", "url is required"} {
		if strings.Contains(text, refusal) {
			t.Fatalf("a valid request was refused with %q: %q", refusal, text)
		}
	}
}
