package main

import (
	"net"
	"strings"
	"testing"
)

func TestIsPrivateIP(t *testing.T) {
	tests := []struct {
		name    string
		ip      string
		private bool
	}{
		// Loopback
		{"loopback v4", "127.0.0.1", true},
		{"loopback v4 high", "127.255.255.255", true},
		{"loopback v6", "::1", true},

		// Private RFC1918
		{"private 10/8", "10.0.0.1", true},
		{"private 10/8 high", "10.255.255.255", true},
		{"private 172.16/12", "172.16.0.1", true},
		{"private 172.31/12", "172.31.255.255", true},
		{"private 192.168/16", "192.168.1.1", true},
		{"private 192.168/16 high", "192.168.255.255", true},

		// Link-local
		{"link-local v4", "169.254.1.1", true},
		{"metadata endpoint", "169.254.169.254", true},
		{"link-local v6", "fe80::1", true},

		// IPv6 unique local
		{"unique local fc00", "fc00::1", true},
		{"unique local fd00", "fd00::1", true},

		// Unspecified
		{"unspecified v4", "0.0.0.0", true},
		{"unspecified v6", "::", true},

		// Public IPs (should NOT be private)
		{"public 8.8.8.8", "8.8.8.8", false},
		{"public 1.1.1.1", "1.1.1.1", false},
		{"public 172.15.255.255", "172.15.255.255", false},
		{"public 172.32.0.0", "172.32.0.0", false},
		{"public 11.0.0.1", "11.0.0.1", false},
		{"public 192.169.0.1", "192.169.0.1", false},
		{"public v6", "2607:f8b0:4004:800::200e", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ip := net.ParseIP(tt.ip)
			if ip == nil {
				t.Fatalf("failed to parse IP: %s", tt.ip)
			}
			got := isPrivateIP(ip)
			if got != tt.private {
				t.Errorf("isPrivateIP(%s) = %v, want %v", tt.ip, got, tt.private)
			}
		})
	}
}

func TestValidateScrapeURL_Scheme(t *testing.T) {
	tests := []struct {
		name    string
		url     string
		wantErr bool
	}{
		{"https ok", "https://example.com", false},
		{"http ok", "http://example.com", false},
		{"ftp rejected", "ftp://example.com/file", true},
		{"file rejected", "file:///etc/passwd", true},
		{"javascript rejected", "javascript:alert(1)", true},
		{"data rejected", "data:text/html,<h1>hi</h1>", true},
		{"no scheme", "example.com", true},
		{"empty", "", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := ValidateScrapeURL(tt.url)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateScrapeURL(%q) error = %v, wantErr = %v", tt.url, err, tt.wantErr)
			}
		})
	}
}

func TestValidateScrapeURL_PrivateIPs(t *testing.T) {
	tests := []struct {
		name    string
		url     string
		wantErr bool
	}{
		{"loopback literal", "http://127.0.0.1/", true},
		{"loopback v6 literal", "http://[::1]/", true},
		{"private 10.x", "http://10.0.0.1/", true},
		{"private 172.16.x", "http://172.16.0.1/", true},
		{"private 192.168.x", "http://192.168.1.1/", true},
		{"link-local", "http://169.254.169.254/latest/meta-data/", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := ValidateScrapeURL(tt.url)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateScrapeURL(%q) error = %v, wantErr = %v", tt.url, err, tt.wantErr)
			}
		})
	}
}

func TestValidateScrapeURL_ReturnsPinnedIP(t *testing.T) {
	ip, err := ValidateScrapeURL("https://example.com")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ip == "" {
		t.Fatal("expected a non-empty pinned IP")
	}
	parsed := net.ParseIP(ip)
	if parsed == nil {
		t.Fatalf("returned IP %q is not a valid IP address", ip)
	}
	if isPrivateIP(parsed) {
		t.Fatalf("returned IP %q is private", ip)
	}
}

func TestParseSubmittedURL(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		host string // expected Hostname; "" means must be rejected
	}{
		{"plain", "https://example.com/a/b?x=1#frag", "example.com"},
		{"port", "http://example.com:8080/x", "example.com"},
		{"userinfo", "https://user:pw@example.com/x", "example.com"},
		{"ipv6 port", "http://[2001:db8::1]:443/x", "2001:db8::1"},
		{"fragment host confusion", "https://github.com#@evil.example/o/r", ""},
		{"fragment host confusion path", "https://github.com#@evil.example", ""},
		{"query host confusion", "https://github.com?@evil.example/o/r", "github.com"},
		{"userinfo with fragment", "https://a@github.com#@evil.example/o/r", ""},
		{"backslash authority", `https://github.com\@evil.example/o/r`, ""},
		{"backslash path", `https://example.com/a\b`, ""},
		{"backslash in query", `https://example.com/s?q=a\b`, "example.com"},
		{"backslash in fragment", `https://example.com/s#a\b`, "example.com"},
		{"fragment directly after host", "https://example.com#top", ""},
		{"fragment colon port", "https://github.com#:80@evil.example/", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			u, err := parseSubmittedURL(tt.raw)
			if tt.host == "" {
				if err == nil {
					t.Fatalf("expected rejection, got host %q", u.Hostname())
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if u.Hostname() != tt.host {
				t.Fatalf("host = %q, want %q", u.Hostname(), tt.host)
			}
		})
	}
}

// Classification must never pick a platform strategy for a URL the validator
// rejects, and must classify accepted URLs by the validated host.
func TestClassifyUsesValidatedHost(t *testing.T) {
	for _, raw := range []string{
		"https://github.com#@evil.example/o/r",
		"https://github.com#@evil.example",
		`https://github.com\@evil.example/o/r`,
		"https://u@github.com#@evil.example/o/r",
	} {
		if _, err := parseSubmittedURL(raw); err == nil {
			t.Errorf("%q should be rejected", raw)
		}
		if d := ClassifyURL(raw); d.Strategy != "" {
			t.Errorf("%q rejected by validator but classified with strategy %q", raw, d.Strategy)
		}
	}
	for _, raw := range []string{
		"https://github.com?@evil.example/o/r", // host github.com, but no owner/repo path
		"https://u@github.com:443/o/r",
	} {
		u, err := parseSubmittedURL(raw)
		if err != nil || u.Hostname() != "github.com" {
			t.Errorf("%q: host=%v err=%v, want github.com", raw, u, err)
		}
	}
	if d := ClassifyURL("https://u@github.com:443/o/r"); d.Strategy != StrategyGitHubRepo {
		t.Errorf("accepted github URL: strategy %q, want %q", d.Strategy, StrategyGitHubRepo)
	}
}

func TestParseSubmittedURLErrorOmitsCredentials(t *testing.T) {
	for _, raw := range []string{
		"https://user:s3cret@example.com:99999x/", // fails url.Parse
		"s3cret@example.com",                      // passes url.Parse, fails url.ParseRequestURI
	} {
		_, err := parseSubmittedURL(raw)
		if err == nil {
			t.Fatalf("%q: expected error", raw)
		}
		if strings.Contains(err.Error(), "s3cret") {
			t.Fatalf("%q: error leaks credentials: %v", raw, err)
		}
	}
}
