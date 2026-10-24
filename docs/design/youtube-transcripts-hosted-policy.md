# YouTube transcripts and the hosted service

Status: decided for now (option 1). Option 3 is a sketch, not scheduled. Written 2026-10-16, after
the transcript worker runtime merged (#37).

## Problem

The transcript worker (`workers/transcript/`) fetches YouTube captions through yt-dlp. YouTube's
Terms of Service restrict automated access to the site, wherever it runs, and requests from
datacenter addresses are also the ones YouTube is most likely to block. Nothing in this repository
has been run from a datacenter address, and the only evidence (the slice-6 spike) is from a home
connection. The official `captions.download` API only works for videos the caller can edit, so it
does not cover arbitrary URLs. Whether the hosted service may offer this is a policy decision for
the owner (with counsel), not something the code settles.

## Decision: option 1, no YouTube access from the hosted service

- The hosted deployment does not start the transcript worker and leaves
  `TRANSCRIPT_WORKER_ENABLED` unset (false). With it off, the gateway does not list
  `extract_transcript` in `tools/list` or `/.well-known/mcp.json` (`gateway/mcp.go`), so clients are
  not offered a tool that cannot work.
- The worker stays available as an opt-in for people who self-host the stack: compose profile
  `transcript`, off by default, with the terms and datacenter warnings at the top of
  `workers/transcript/README.md`. A self-hoster is responsible for their own use.
- Hosted users keep the other tools (scrape, crawl, extract, GitHub, Hacker News).

Keeping this true:

- Never default the flag or the compose profile on.
- A deployment checklist item for the hosted service: transcript worker not running, flag false.
- Do not add anything that hides where requests come from (proxy pools, IP rotation, spoofed
  headers, cookies from real accounts). Datacenter blocking is YouTube's signal; hiding from it makes
  the terms position worse, not better.
- Revisit only with (a) a real run from the intended provider and region, and (b) a written
  position on the terms from the owner or counsel.

## Option 3 (not scheduled): the user supplies the transcript

A hosted tool that never contacts YouTube: the caller brings the text, the service formats it.

Phase A, caption files. A tool such as `format_transcript` accepts a WebVTT caption file (SRT would
need its own parser) that the user obtained themselves, and returns Markdown in the same shape the
worker produces: timestamped lines inside a code fence that no backtick run in the text can close,
size capped at 1 MiB. `parse_vtt` is pure and can be reused unchanged. `render_transcript` is pure
too but is not reusable as it stands: it requires a valid video id, track kind and language, and
always writes a `Video: <youtube url>` line. It would need a variant for files without those (no
video line, an optional label in place of the track description), keeping the fence and sanitising
code shared. No network, no cache by video id (there is no id), no worker consumer. Open
questions: the upload shape (the API today takes JSON, so a size limit and encoding decide between
inline text and a multipart upload), whether to accept a video title as a plain string, and how long
anything is retained (the answer should be "not at all").

Phase B, audio. The user uploads audio they hold the rights to and the AI layer (`ai/`) transcribes
it. Larger: it needs a speech-to-text model or vendor, file size and duration limits, a job type with
its own worker, and a cost model. Decide on phase A's usage first.

A related, cheaper form: document running the worker on the user's own machine against a local
stack, so the access is theirs and the hosted service is not involved. Redis is not exposed by the
hosted service, so this means self-hosting the whole stack, not pointing a local worker at the hosted
one.

## Not planned

- Option 2: the official API for the user's own videos (OAuth, `captions.download`). Only worth
  building if users ask for creator-owned videos.
- A licensed third-party transcript provider or a permission agreement with YouTube. Business
  decisions; the `CaptionSource` seam in the worker is where a provider would plug in.
