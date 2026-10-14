package main

import (
	"net"
	"net/http"
	"testing"

	"github.com/gofiber/fiber/v2"
)

func skipWithoutIPv6(t *testing.T) {
	t.Helper()
	ln, err := net.Listen("tcp6", "[::1]:0")
	if err != nil {
		t.Skipf("IPv6 loopback unavailable: %v", err)
	}
	ln.Close()
}

// Fly's .internal (6PN) addresses are IPv6-only, so the gateway must accept
// IPv6 connections (and keep accepting IPv4).
func TestGatewayListensDualStack(t *testing.T) {
	skipWithoutIPv6(t)

	queue := NewQueueClient("127.0.0.1:1") // lazy: nothing dials Redis in this test
	defer queue.Close()
	app := NewApp(Config{}, queue)

	if got := app.Config().Network; got != fiber.NetworkTCP {
		t.Fatalf("Network = %q, want %q (dual-stack)", got, fiber.NetworkTCP)
	}

	// Bind exactly the way app.Listen(":port") does: the configured network, wildcard host.
	ln, err := net.Listen(app.Config().Network, ":0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = app.Listener(ln) }()
	defer app.Shutdown()

	_, port, _ := net.SplitHostPort(ln.Addr().String())
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	for _, host := range []string{"[::1]", "127.0.0.1"} {
		// Static route: /health pings Redis, which is not running here.
		resp, err := client.Get("http://" + host + ":" + port + "/.well-known/mcp.json")
		if err != nil {
			t.Fatalf("GET via %s: %v", host, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("GET via %s: status %d", host, resp.StatusCode)
		}
	}
}
