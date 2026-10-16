<p align="center">
  <img src="assets/cheetah-logo.png" alt="Cheetah" width="400">
</p>

<h1 align="center">Cheetah</h1>

<p align="center">The next-generation web data engine for AI and RAG applications.</p>

## Architecture

Cheetah uses a **Smart Routing Architecture** with tiered workers to minimize compute costs:

- **Gateway** (`gateway/`) — Go/Fiber API handling REST requests, rate limiting, and auth
- **Smart Router** (`router/`) — Python service that classifies URLs and routes to the right tier
- **Tier 1: Fast Scraper** (`workers/fast/`) — Rust worker for static pages (reqwest + scraper)
- **Tier 2: Browser Scraper** (`workers/browser/`) — Node.js/Playwright for JS-rendered pages
- **Tier 3: Stealth Scraper** (`workers/stealth/`) — Proxy-based scraping for protected sites
- **Transcript Worker** (`workers/transcript/`) — Python worker for YouTube transcripts (opt-in, off by default; see its README)
- **AI Layer** (`ai/`) — Python/vLLM for structured extraction and semantic chunking

## Quick Start

Run the full stack (gateway + fast worker + Redis) with one command:

```bash
docker compose up
```

The API will be available at `http://localhost:3000`.

## Development

Each component has its own build tooling:

```bash
# Gateway (Go)
cd gateway && go run .

# Fast worker (Rust)
cd workers/fast && cargo run

# Browser worker (Node.js)
cd workers/browser && npm install && npm run dev

# Transcript worker (Python 3.12.4+; opt-in, see workers/transcript/README.md)
cd workers/transcript && pip install -r requirements-dev.txt && python -m pytest
```

## API Endpoints

- `POST /v1/scrape` — Scrape a single URL
- `POST /v1/crawl` — Crawl a site recursively
- `POST /v1/extract` — Extract structured data via JSON schema
