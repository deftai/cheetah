package main

import (
	"net/url"
	"regexp"
	"strings"
	"unicode"
)

const (
	// StrategyYouTubeTranscript names the transcript worker's extractor. Unlike
	// the router strategies it is never produced by ClassifyURL: YouTube URLs
	// sent to scrape keep being scraped, and only the extract_transcript tool
	// (when the transcript worker is enabled) routes to transcript_jobs.
	StrategyYouTubeTranscript = "youtube_transcript"

	transcriptStream = "transcript_jobs"

	transcriptAccepts = "a YouTube video URL (https://www.youtube.com/watch?v=<id>, https://youtu.be/<id>, or a /shorts/, /embed/ or /live/ video link)"
)

func isYouTubeID(s string) bool {
	if len(s) != 11 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
			return false
		}
	}
	return true
}

func isYouTubeHost(host string) bool {
	switch host {
	case "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be":
		return true
	}
	return false
}

// submittedHost is the lowercased hostname of a submitted URL, or "" when it
// does not parse (it uses the shared parse, so ambiguous URLs have no host).
func submittedHost(rawURL string) string {
	u, err := parseSubmittedURL(rawURL)
	if err != nil {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

// YouTubeVideoID returns the video id of a supported YouTube video URL:
// https://www.youtube.com/watch?v=<id> (also youtube.com and m.youtube.com),
// https://youtu.be/<id>, and the /shorts/<id>, /embed/<id> and /live/<id> forms.
// Only the 11-character id is trusted from the submitted URL: the transcript
// worker rebuilds the canonical URL itself, so nothing else in it (query
// parameters, fragment, path decoration) is ever forwarded. The URL is held to
// the same standard as the other platform tools: https only, the shared
// unambiguous parse, no whitespace or control characters, no userinfo, only
// the default port, and a path read as written with no dot segments.
func YouTubeVideoID(rawURL string) (string, bool) {
	for _, r := range rawURL {
		if unicode.IsSpace(r) || unicode.IsControl(r) {
			return "", false
		}
	}
	u, err := parseSubmittedURL(rawURL)
	if err != nil || u.Scheme != "https" || u.User != nil {
		return "", false
	}
	if p := u.Port(); p != "" && p != "443" {
		return "", false
	}
	host := strings.ToLower(u.Hostname())
	if !isYouTubeHost(host) {
		return "", false
	}
	path, ok := rawPathOf(rawURL)
	if !ok {
		return "", false
	}
	segs, ok := rawSegments(path)
	if !ok {
		return "", false
	}

	var id string
	switch {
	case host == "youtu.be":
		if len(segs) != 1 {
			return "", false
		}
		id = segs[0]
	case len(segs) == 1 && segs[0] == "watch":
		q, err := url.ParseQuery(u.RawQuery)
		if err != nil || len(q["v"]) == 0 {
			return "", false
		}
		id = q["v"][0]
	case len(segs) == 2 && (segs[0] == "shorts" || segs[0] == "embed" || segs[0] == "live"):
		id = segs[1]
	default:
		return "", false
	}
	if !isYouTubeID(id) {
		return "", false
	}
	return id, true
}

func canonicalYouTubeURL(id string) string {
	return "https://www.youtube.com/watch?v=" + id
}

// transcriptLanguagePattern accepts caption language codes as yt-dlp names
// them: a 2-3 letter language with optional subtags (en, pt-BR, zh-Hans) and
// the original-speech form (ko-orig).
var transcriptLanguagePattern = regexp.MustCompile(`^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$`)

func validTranscriptLanguage(s string) bool {
	return len(s) <= 20 && transcriptLanguagePattern.MatchString(s)
}

// transcriptURLError returns the video id for a supported URL, or a message
// saying what extract_transcript accepts and where the URL belongs. It never
// repeats the submitted URL, which may carry secrets in its query.
func transcriptURLError(rawURL string) (string, string) {
	if id, ok := YouTubeVideoID(rawURL); ok {
		return id, ""
	}
	if isYouTubeHost(submittedHost(rawURL)) {
		return "", "extract_transcript only accepts " + transcriptAccepts + "; this page type or URL form is not supported, use \"scrape\" for it"
	}
	hint := `use "scrape" for other pages`
	switch ClassifyURL(rawURL).Strategy {
	case StrategyGitHubRepo:
		hint = `this URL belongs to "get_repository_content"`
	case StrategyHNThread:
		hint = `this URL belongs to "get_thread"`
	}
	return "", "extract_transcript only accepts " + transcriptAccepts + "; " + hint
}
