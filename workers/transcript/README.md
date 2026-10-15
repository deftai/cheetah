# Transcript worker

Fetches YouTube captions and returns them as Markdown. It consumes the `transcript_jobs` stream
(consumer group `transcript-workers`) that the gateway's `extract_transcript` MCP tool writes to.
Python 3.12.4 or newer; captions come from yt-dlp used as a library.

It is **opt-in and off by default**: the compose service sits behind a profile, and the gateway
shows the `extract_transcript` tool only when `TRANSCRIPT_WORKER_ENABLED=true`.

## Read this before enabling it anywhere

- **YouTube's Terms of Service** restrict automated access to the site. Whether to offer transcripts
  from a paid or hosted service is a policy decision this code does not make.
- **It has not been run from a datacenter or cloud address.** All the evidence (slice-6 spike) comes
  from a home connection and one VPN of unrecorded type: manual and native auto captions worked, a
  machine-translated track got HTTP 429 while the video's native track worked. Whether the same
  requests succeed from your provider and region is open. Do not assume they do.
- **There is no fallback.** A job that cannot produce a transcript fails with an error code and a
  message; it is never re-routed to a plain page scrape.

## Run it

```bash
# 1. Start Redis, the gateway and the worker (the worker needs the profile):
docker compose --profile transcript up -d --build

# 2. Tell the gateway a worker exists (.env or the shell):
TRANSCRIPT_WORKER_ENABLED=true docker compose --profile transcript up -d gateway
```

`GET /v1/health` then lists a `transcript` worker tier. The gateway counts a consumer as running
when it was seen in the last 30 s, and refuses `extract_transcript` jobs when none is.

Without Docker, from this directory: `pip install -r requirements.txt` and
`REDIS_URL=redis://127.0.0.1:6379 python -m transcript_worker`.

### Settings (environment)

| Variable | Default | |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379` | Never echoed in errors or logs. |
| `TRANSCRIPT_CACHE_TTL_SECONDS` | `86400` | How long a successful transcript is cached. `0` turns the cache off. |
| `TRANSCRIPT_RATE_INTERVAL_SECONDS` | `3` | Minimum seconds between two requests to YouTube from one worker. |
| `TRANSCRIPT_CONCURRENCY` | `2` | Jobs at once (1-8). |
| `TRANSCRIPT_JOB_TIMEOUT_SECONDS` | `90` | Time one job may take, retries included (10-100). |
| `TRANSCRIPT_MAX_ATTEMPTS` | `3` | Tries per YouTube request when rate limited or timed out (1-5). |
| `TRANSCRIPT_CONSUMER_NAME` | `transcript-<hostname>` | Stable name of this replica in the group. |

A bad value stops the worker at start, naming the variable.

## How it behaves

- **Track choice:** the creator's captions in the requested language; else the native auto-generated
  track (`<lang>-orig` when present); else a machine-translated track, best effort. With no language
  given, the video's original spoken language. Translated tracks are not retried when YouTube refuses
  them; the error says to ask for the original language.
- **Errors** (`error.code`): `no_captions`, `no_track_in_language`, `rate_limited`, `blocked`,
  `unavailable`, `timeout`, `internal`. Messages are written by the worker; no yt-dlp or YouTube
  text reaches a result.
- **Cache:** successes only. An entry is stored under video id + the chosen track, plus a small
  pointer from (video id, requested language) to it, so a repeat request does not contact YouTube at
  all, and two requests that resolve to the same track share one entry. The cache holds third-party
  caption text: `TRANSCRIPT_CACHE_TTL_SECONDS` is also its retention. Keys are `transcript_cache:v1:*`.
  A Redis outage on the cache is a miss, never a failed job.
- **Redis memory:** an entry can be up to 1 MiB (typically 50-100 KB), and at the default rate one
  worker can add about 29,000 entries a day, so the cache can grow by gigabytes a day under a diverse
  workload. It shares Redis with the job stream and results. Give that Redis a limit that evicts only
  keys that expire, for example `redis-server --maxmemory 512mb --maxmemory-policy volatile-lru` (the
  stream has no TTL; cache entries and results all have one). Not `allkeys-lru`: it would evict the
  stream. The compose file does not set this for you.
- **Erasing cached text** (a takedown or an erasure request): the cache is plain Redis keys, and the
  text is also in Redis's snapshot or append-only files until they are rewritten:
  `redis-cli --scan --pattern 'transcript_cache:v1:*:<video_id>:*' | xargs redis-cli del`.
- **Rate limiting:** one token bucket per worker process (burst 1). Every request to YouTube (the
  track listing, the caption download) takes a token. yt-dlp's own retries are off; the worker
  retries rate-limited and timed-out requests with jittered backoff, never sooner than a
  `Retry-After`, never past the job's time limit.
- **Delivery:** at least once. An entry is acked after its result is written; entries a dead worker
  left pending are claimed after 2 minutes; an entry delivered more than 3 times is failed instead of
  retried. Replicas that are gone (idle over 10 minutes, nothing pending) are removed from the group,
  and a worker deletes its own consumer on a clean shutdown.
- **Liveness:** the event loop only awaits Redis, so `XREADGROUP` (BLOCK 2 s) keeps being called
  while jobs run in the thread pool, and keeps being called (reading its own pending history, taking
  no job) when every slot is busy.

### Known limits

- A job that hangs inside yt-dlp cannot be killed (it is a thread). The caller still gets a
  `timeout` result, and the stuck thread keeps its slot: the worker stops taking jobs for it (they
  wait in the stream) until the thread returns, and with every thread stuck it takes none until the
  container restarts. yt-dlp's socket timeout (20 s) bounds each network step, which makes this
  unlikely, not impossible.
- **Egress is not restricted by the worker, and `resolved_ip` is not used.** The worker validates the
  gateway's `resolved_ip` but yt-dlp does its own DNS, so nothing pins `www.youtube.com`. yt-dlp also
  contacts other Google hosts and follows redirects. Caption URLs are checked to be `https` on
  `youtube.com`, but the deployment still needs its own egress policy (network policy or firewall)
  if the worker runs next to anything private.
- No cookies, no proxy, no PO token, no JS runtime. Age-restricted, private and members-only videos
  report `unavailable`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest                      # offline; Redis is faked (fakeredis)
ruff check . && mypy transcript_worker   # strict, non-test modules
```

Two opt-in suites run against the real thing; both are skipped unless you ask:

```bash
# Real Redis semantics (idle time while busy, XAUTOCLAIM, DELCONSUMER). Use a throwaway Redis: the
# test uses the fixed stream name transcript_jobs and refuses to run if that key already exists.
docker run --rm -d --name tw-redis -p 6390:6379 redis:7-alpine
TRANSCRIPT_TEST_REDIS_URL=redis://127.0.0.1:6390 python -m pytest -m redis -v
docker stop tw-redis

# Real YouTube (a handful of requests for one short public video). Run it from the network you
# intend to deploy to; a pass at home proves nothing about a datacenter.
TRANSCRIPT_LIVE_TEST=1 python -m pytest -m live -v
```

Run the live test after every yt-dlp bump. It is the only thing that notices YouTube changing under
the pin.
