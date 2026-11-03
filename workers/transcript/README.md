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

The hosted service does not run this worker. The reasoning, the guardrails that keep it that way and
the alternative being considered are in
[`docs/design/youtube-transcripts-hosted-policy.md`](../../docs/design/youtube-transcripts-hosted-policy.md).

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
  text reaches a result. `timeout` also covers YouTube not being reachable at all (no route, DNS failure, a proxy that
  will not tunnel), so a worker with no egress reports that rather than `internal`.
- **Cache:** successes only. An entry is stored under video id + the chosen track, plus a small
  pointer from (video id, requested language) to it, so a repeat request does not contact YouTube at
  all, and two requests that resolve to the same track share one entry. The cache holds third-party
  caption text: `TRANSCRIPT_CACHE_TTL_SECONDS` is also its retention. Keys are `transcript_cache:v1:*`.
  A Redis outage on the cache is a miss, never a failed job.
- **Redis memory:** an entry can be up to 1 MiB (typically 50-100 KB), and at the default rate one
  worker can add about 14,000 entries a day (two requests to YouTube per new entry), so the cache can grow by gigabytes a day under a diverse
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

## Keeping yt-dlp working

YouTube changes without notice and yt-dlp follows within days, so the usual way this worker breaks is a
pin that fell behind. Two checks cover it; they answer different questions.

| Check | Contacts YouTube | Answers |
|---|---|---|
| `python -m transcript_worker.pin_age` | No (PyPI only) | Is the pin old while a newer release exists? |
| `python -m transcript_worker.canary` | Yes, two operations for one public video | Does the pinned yt-dlp still fetch captions from this network? |

**`pin_age`** runs weekly in CI (`.github/workflows/transcript-pin-age.yml`) and fails once the pinned
release is older than 45 days and a newer one exists. A PyPI outage warns and passes.

**`canary`** is not scheduled by this repository. Whoever runs the worker schedules it, from the
network the worker uses: a pass from a laptop or a CI runner says nothing about a datacenter address.
It is automated access to YouTube, under the same Terms and datacenter caveats as the worker itself
(see the top of this file), so it is for self-hosters who have turned the worker on. Do not schedule it on the
hosted deployment, which does not run the worker
([`docs/design/youtube-transcripts-hosted-policy.md`](../../docs/design/youtube-transcripts-hosted-policy.md)).
It makes two operations (list the tracks, download one caption file) and does not retry. A limit of 150 s covers
the whole run: if a request blocks on the network, the canary prints an `inconclusive` verdict with code `timeout` and
exits 2, so the cron line still reports (a hang inside native code that holds the GIL is not covered). Its stdout is one JSON line
(worker-written text and the yt-dlp version only); stderr may carry short, scrubbed yt-dlp diagnostics, so treat the
log as holding some third-party text. It exits:

| Exit | `status` | Meaning | Do |
|---|---|---|---|
| 0 | `ok` | A real transcript came back. | Nothing. |
| 1 | `broken` | The request went through and the answer was wrong (no captions on a video that has them, a file that no longer parses, an unclassified error). | Bump yt-dlp, below. |
| 2 | `inconclusive` | `blocked`, `rate_limited`, `timeout` or `unavailable`: YouTube refused this network or the moment. | One run is not an alert. Several in a row from a network that used to pass: that network is blocked, which a yt-dlp bump will not fix. |

```bash
# docker compose (--no-deps: the canary does not need Redis)
docker compose --profile transcript run --rm --no-deps transcript-worker python -m transcript_worker.canary
# a crontab line, daily, with the worker's own image or virtualenv: alert on anything but 0 and 2 (exit 1, or a
# canary that did not run at all, such as 127 or 137); exit 2 is only logged, so scan the log for a run of them
17 5 * * *  cd /opt/cheetah/workers/transcript && python -m transcript_worker.canary >> /var/log/transcript-canary.log 2>&1; rc=$?; [ $rc -eq 0 ] || [ $rc -eq 2 ] || notify-me
```

Run it at most once a day.

### Bumping yt-dlp

1. Read the yt-dlp release notes since the pinned version for YouTube extractor fixes.
2. Change the pin in `requirements.txt` in a change of its own, nothing else in it.
3. Run the offline tests, then `TRANSCRIPT_LIVE_TEST=1 python -m pytest -m live -v` and the canary, from the
   network you deploy to.
4. If captions now need a JavaScript runtime (the Dockerfile says none is installed on purpose), the live test
   fails with `internal` or `no_captions`; that is a design change, not a bump.
5. Rebuild the image and restart the worker.

If the newest yt-dlp still fails and YouTube changed something the library has not caught up with, turn the
feature off (`TRANSCRIPT_WORKER_ENABLED=false` on the gateway) rather than adding retries or workarounds here.

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

# Real YouTube (a handful of requests for two public videos). Run it from the network you
# intend to deploy to; a pass at home proves nothing about a datacenter.
TRANSCRIPT_LIVE_TEST=1 python -m pytest -m live -v
```

Run the live test after every yt-dlp bump. With the canary (see "Keeping yt-dlp working"), it is what
notices YouTube changing under the pin.
