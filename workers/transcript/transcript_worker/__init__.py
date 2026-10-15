"""The YouTube transcript worker.

Two layers. The core (errors, validators, tracks, captions, render, ratelimit)
is pure logic: no network, no Redis. The runtime sits on top of it: config,
cache, source and pipeline (the job's logic, behind a `CaptionSource` seam so
it is testable without YouTube), results and handler (a stream entry to a
result payload), and the I/O edges: `ytdlp_source` (yt-dlp), `consumer`
(Redis stream) and `main` (wiring). Everything except those three edges is
testable offline.
"""
