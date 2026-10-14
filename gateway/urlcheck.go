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
			return "", fmt.Errorf("url resolves to an unparseable IP address")
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

// reservedNets are non-public ranges that net.IP's helpers do not cover; see
// the IANA IPv4/IPv6 Special-Purpose Address Registries (RFC 6890, RFC 8190).
// ISATAP (::5efe:a.b.c.d under any prefix) and operator-specific NAT64
// prefixes embed IPv4 in forms that cannot be judged from the address alone.
var reservedNets = mustParseCIDRs(
	"0.0.0.0/8",       // "this network" (beyond 0.0.0.0 itself)
	"100.64.0.0/10",   // CGNAT; some clouds expose metadata here
	"192.0.0.0/24",    // IETF protocol assignments
	"198.18.0.0/15",   // benchmarking
	"240.0.0.0/4",     // reserved, includes 255.255.255.255 broadcast
	"192.88.99.0/24",  // 6to4 relay anycast
	"192.0.2.0/24",    // documentation
	"198.51.100.0/24", // documentation
	"203.0.113.0/24",  // documentation
	"::/96",           // IPv4-compatible ::a.b.c.d (also ::, ::1)
	"::ffff:0:0:0/96", // SIIT-translated IPv4
	"64:ff9b:1::/48",  // local-use NAT64 (RFC 8215)
	"2001::/32",       // Teredo
	"2001:2::/48",     // benchmarking
	"2001:db8::/32",   // documentation
	"3fff::/20",       // documentation (RFC 9637)
	"5f00::/16",       // SRv6 SIDs
	"100::/64",        // discard-only
	"fec0::/10",       // deprecated site-local
)

// IPv6 transition ranges that embed an IPv4 address, which is judged instead.
var (
	nat64Net = mustParseCIDR("64:ff9b::/96")
	sixToNet = mustParseCIDR("2002::/16")
)

func mustParseCIDR(c string) *net.IPNet {
	_, n, err := net.ParseCIDR(c)
	if err != nil {
		panic(err)
	}
	return n
}

func mustParseCIDRs(cidrs ...string) []*net.IPNet {
	nets := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		nets = append(nets, mustParseCIDR(c))
	}
	return nets
}

// isPrivateIP returns true if ip falls within any private, loopback,
// link-local, multicast, or otherwise reserved range that should not be
// reachable from server-side requests. NAT64 and 6to4 addresses are judged by
// the IPv4 address they embed.
func isPrivateIP(ip net.IP) bool {
	if ip.IsLoopback() ||
		ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() ||
		ip.IsMulticast() ||
		ip.IsUnspecified() {
		return true
	}
	for _, n := range reservedNets {
		if n.Contains(ip) {
			return true
		}
	}
	if ip.To4() != nil {
		return false
	}
	ip16 := ip.To16()
	switch {
	case ip16 == nil:
		return true // not a valid address: fail closed
	case nat64Net.Contains(ip):
		return isPrivateIP(net.IP(ip16[12:16]))
	case sixToNet.Contains(ip):
		return isPrivateIP(net.IP(ip16[2:6]))
	}
	return false
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
