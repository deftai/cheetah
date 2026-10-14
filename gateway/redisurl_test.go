package main

import (
	"strings"
	"testing"
)

func TestCheckRedisURL(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		want    string
		wantErr bool
	}{
		{"valid url", "redis://default:pw@host.example:6379", "redis://default:pw@host.example:6379", false},
		// The bug this guards: a trailing space made ParseURL fail with "invalid
		// port", and the old fallback dialed the whole string as a host.
		{"trailing space is trimmed", "redis://default:pw@host.example:6379 ", "redis://default:pw@host.example:6379", false},
		{"surrounding whitespace is trimmed", "  redis://host.example:6379\n", "redis://host.example:6379", false},
		{"bare host:port passes through", "localhost:6379", "localhost:6379", false},
		{"bad port in a url is an error", "redis://default:pw@host.example:notaport", "", true},
		{"unsupported scheme is an error", "http://default:pw@host.example:6379", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := checkRedisURL(tc.in)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

// The password must never appear in the error: it ends up in the startup log.
func TestCheckRedisURLErrorDoesNotLeakCredentials(t *testing.T) {
	_, err := checkRedisURL("redis://default:s3cr3tpassw0rd@host.example:notaport")
	if err == nil {
		t.Fatal("expected an error")
	}
	for _, leak := range []string{"s3cr3tpassw0rd", "host.example", "default"} {
		if strings.Contains(err.Error(), leak) {
			t.Fatalf("error leaks %q: %v", leak, err)
		}
	}
}

func TestNewQueueClientTrimsAddress(t *testing.T) {
	q := NewQueueClient("redis://default:pw@host.example:6379 ")
	defer q.Close()
	if got := q.rdb.Options().Addr; got != "host.example:6379" {
		t.Fatalf("Addr = %q, want host.example:6379 (the trailing space must not reach the dialer)", got)
	}
}
