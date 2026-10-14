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

		// Reserved ranges beyond net.IP's helpers
		{"this-network 0.1.2.3", "0.1.2.3", true},
		{"cgnat low", "100.64.0.0", true},
		{"cgnat metadata", "100.100.100.200", true},
		{"cgnat high", "100.127.255.255", true},
		{"ietf 192.0.0.1", "192.0.0.1", true},
		{"benchmark 198.18.0.1", "198.18.0.1", true},
		{"benchmark high", "198.19.255.255", true},
		{"reserved 240.0.0.1", "240.0.0.1", true},
		{"broadcast", "255.255.255.255", true},
		{"multicast v4", "224.0.0.1", true},
		{"multicast v4 global", "239.255.255.250", true},
		{"multicast v6", "ff02::1", true},
		{"multicast v6 global", "ff0e::1", true},

		// IPv4-mapped and transition addresses embedding private IPv4
		{"v4-mapped loopback", "::ffff:127.0.0.1", true},
		{"v4-mapped metadata", "::ffff:169.254.169.254", true},
		{"nat64 loopback", "64:ff9b::7f00:1", true},
		{"nat64 10/8", "64:ff9b::a00:1", true},
		{"nat64 cgnat", "64:ff9b::6440:1", true},
		{"6to4 loopback", "2002:7f00:1::1", true},
		{"6to4 192.168", "2002:c0a8:101::1", true},

		// Other IPv6 forms that alias or embed IPv4, and unroutable ranges
		{"site-local", "fec0::1", true},
		{"ipv4-compatible", "::7f00:1", true},
		{"siit", "::ffff:0:7f00:1", true},
		{"teredo", "2001:0:4136:e378:8000:63bf:3fff:fdd2", true},
		{"local-use nat64", "64:ff9b:1::1", true},
		{"discard", "100::1", true},
		{"6to4 relay anycast", "192.88.99.1", true},
		{"doc 192.0.2.1", "192.0.2.1", true},
		{"doc 198.51.100.1", "198.51.100.1", true},
		{"doc 203.0.113.1", "203.0.113.1", true},
		{"doc v6", "2001:db8::1", true},
		{"6to4 metadata", "2002:a9fe:a9fe::", true},
		{"nat64 benchmark", "64:ff9b::c612:1", true},
		{"ietf upper edge", "192.0.0.255", true},

		// Public IPs (should NOT be private)
		{"public 192.0.1.0", "192.0.1.0", false},
		{"public google dns v6", "2001:4860:4860::8888", false},
		{"public 2001:1::1 outside teredo", "2001:1::1", false},
		{"public above benchmarking v6", "2001:2:1::1", false},
		{"public above documentation 3fff", "3fff:1000::1", false},
		{"public below srv6", "5eff::1", false},
		{"public above srv6", "5f01::1", false},
		{"public cloudflare v6", "2606:4700::1111", false},
		{"public 2001:3::1 (outside 2001:2::/48)", "2001:3::1", false},
		{"public 64:ff9b:2::1", "64:ff9b:2::1", false},
		{"public 100:0:0:1::1", "100:0:0:1::1", false},
		{"public 2001:4860::1", "2001:4860::1", false},
		{"v4-mapped public cgnat-adjacent", "::ffff:100.128.0.1", false},
		{"v4-mapped cgnat", "::ffff:100.64.0.1", true},
		{"v4-mapped documentation", "::ffff:192.0.2.1", true},
		{"benchmarking v6", "2001:2::1", true},
		{"documentation 3fff", "3fff::1", true},
		{"srv6", "5f00::1", true},
		{"siit blocked regardless of embedded v4", "::ffff:0:808:808", true},
		{"benchmarking v6 upper edge", "2001:2:0:ffff::1", true},
		{"documentation 3fff upper edge", "3fff:fff:ffff::", true},
		{"public below cgnat", "100.63.255.255", false},
		{"public above cgnat", "100.128.0.0", false},
		{"public below benchmark", "198.17.255.255", false},
		{"public above benchmark", "198.20.0.0", false},
		{"public 223.255.255.255", "223.255.255.255", false},
		{"public 1.0.0.1", "1.0.0.1", false},
		{"v4-mapped public", "::ffff:8.8.8.8", false},
		{"nat64 public", "64:ff9b::808:808", false},
		{"6to4 public", "2002:808:808::1", false},
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

func TestIsPrivateIPInvalidFailsClosed(t *testing.T) {
	for _, ip := range []net.IP{nil, {1, 2, 3}} {
		if !isPrivateIP(ip) {
			t.Errorf("isPrivateIP(%v) = false, want true (fail closed)", ip)
		}
	}
	if isPrivateIP(net.IPv4(8, 8, 8, 8).To4()) { // raw 4-byte form
		t.Error("public 4-byte 8.8.8.8 reported private")
	}
	if !isPrivateIP(net.IP{100, 100, 100, 200}) {
		t.Error("4-byte CGNAT metadata address not blocked")
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
		{"query then fragment @", "https://example.com?x#@evil", "example.com"},
		{"backslash before query", `https://h\?x`, ""},
		{"backslash before fragment", `https://h\#x`, ""},
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
