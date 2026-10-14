package main

import (
	"errors"
	"fmt"
	"log"
	"net"
	"net/url"
	"strings"
)

// parseSubmittedURL is the single parse shared by classification and SSRF
// validation. url.Parse splits off the fragment before reading the authority
// while url.ParseRequestURI does not, so inputs such as
// "https://github.com#@evil.example/o/r" yield different hosts. Such URLs are
// ambiguous (other HTTP clients may disagree too) and are rejected: the URL
// must parse identically under both parsers, and its authority/path (the part
// before any query or fragment) must contain no backslashes, which WHATWG-style
// clients treat as "/".
func parseSubmittedURL(rawURL string) (*url.URL, error) {
	if strings.ContainsRune(beforeQueryOrFragment(rawURL), '\\') {
		return nil, errors.New("url contains backslash")
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, redactURLError(err)
	}
	r, err := url.ParseRequestURI(rawURL)
	if err != nil {
		return nil, redactURLError(err)
	}
	if u.Scheme != r.Scheme || u.Host != r.Host || u.User.String() != r.User.String() {
		return nil, errors.New("ambiguous url authority")
	}
	return u, nil
}

func beforeQueryOrFragment(rawURL string) string {
	if i := strings.IndexAny(rawURL, "?#"); i >= 0 {
		return rawURL[:i]
	}
	return rawURL
}

// ValidateScrapeURL validates that rawURL is safe for server-side fetching.
// It rejects non-http(s) schemes and URLs that resolve to private/reserved IPs.
// On success it returns the first validated IP so callers can pin it in the job
// payload, closing the TOCTOU window between validation and worker fetch.
func ValidateScrapeURL(rawURL string) (string, error) {
	u, err := parseSubmittedURL(rawURL)
	if err != nil {
		log.Printf("url validation failed: %v", err)
		return "", fmt.Errorf("invalid url")
	}

	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("url scheme must be http or https")
	}

	host := u.Hostname()
	if host == "" {
		return "", fmt.Errorf("url must include a hostname")
	}

	ips, err := net.LookupHost(host)
	if err != nil {
		return "", fmt.Errorf("cannot resolve hostname: %s", host)
	}

	var pinnedIP string
	for _, ipStr := range ips {
		ip := net.ParseIP(ipStr)
		if ip == nil {
			continue
		}
		if isPrivateIP(ip) {
			return "", fmt.Errorf("url resolves to a private or reserved IP address")
		}
		if pinnedIP == "" {
			pinnedIP = ipStr
		}
	}

	if pinnedIP == "" {
		return "", fmt.Errorf("cannot resolve hostname: %s", host)
	}

	return pinnedIP, nil
}

// isPrivateIP returns true if ip falls within any private, loopback,
// link-local, or otherwise reserved range that should not be reachable
// from server-side requests.
func isPrivateIP(ip net.IP) bool {
	return ip.IsLoopback() ||
		ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified()
}

// redactURLError drops the quoted input that *url.Error embeds, since the
// submitted URL may carry credentials (userinfo) or tokens that must not be logged.
func redactURLError(err error) error {
	var ue *url.Error
	if errors.As(err, &ue) {
		return fmt.Errorf("%s: %w", ue.Op, ue.Err)
	}
	return err
}
