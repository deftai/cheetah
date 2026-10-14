package main

import (
	"fmt"
	"log"
	"net"
	"net/url"
)

// ValidateScrapeURL validates that rawURL is safe for server-side fetching.
// It rejects non-http(s) schemes and URLs that resolve to private/reserved IPs.
// On success it returns the first validated IP so callers can pin it in the job
// payload, closing the TOCTOU window between validation and worker fetch.
func ValidateScrapeURL(rawURL string) (string, error) {
	u, err := url.ParseRequestURI(rawURL)
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

// reservedNets are non-public ranges that net.IP's helpers do not cover.
var reservedNets = mustParseCIDRs(
	"0.0.0.0/8",     // "this network" (beyond 0.0.0.0 itself)
	"100.64.0.0/10", // CGNAT; some clouds expose metadata here
	"192.0.0.0/24",  // IETF protocol assignments
	"198.18.0.0/15", // benchmarking
	"240.0.0.0/4",   // reserved, includes 255.255.255.255 broadcast
)

// IPv6 transition ranges that embed an IPv4 address, which is judged instead.
var (
	nat64Net = mustParseCIDRs("64:ff9b::/96")[0]
	sixToNet = mustParseCIDRs("2002::/16")[0]
)

func mustParseCIDRs(cidrs ...string) []*net.IPNet {
	nets := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic(err)
		}
		nets = append(nets, n)
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
