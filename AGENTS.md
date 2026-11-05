# Cheetah

Polyglot web data engine: Go gateway, Rust scraper, Python AI router/extractor, Node.js browser worker, Python transcript worker.

## Architecture

```
gateway/        Go — HTTP API, URL validation, job routing
workers/fast/   Rust — headless scraping (reqwest)
workers/browser/ Node.js — browser-based scraping (Playwright)
workers/transcript/ Python — transcript extraction
ai/             Python — LLM-based structured extraction (OpenAI-compatible API)
router/         Python — request routing and AI orchestration
```

## Build & test

Each component has its own toolchain:

```sh
# Gateway (Go)
cd gateway && go build -o gateway . && go test ./...

# Fast worker (Rust)
cd workers/fast && cargo build && cargo test

# Browser worker (Node.js)
cd workers/browser && npm install && npm test

# AI extractor (Python)
cd ai && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest test_extractor.py -v

# Transcript worker (Python)
cd workers/transcript && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -v

# Router (Python)
cd router && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -v
```

## Conventions

- Feature branches off `main`; squash-merge PRs.
- No `Status`, `Next`, or `Known Issues` blocks in this file.
- Fix what a quality gate catches — don't weaken the gate.
- Multi-step automated work needs both a success condition and a stop condition (iteration cap or budget).
- Record significant decisions in PR descriptions, not ephemeral docs.

## Scope records

`xbrief/` contains historical scope records (what was built and why). They are read-only history, not a work queue.
