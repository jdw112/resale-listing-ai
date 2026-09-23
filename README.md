<div align="center">

# resale-listing-ai

**Turn resale product photos into priced, ready-to-list records — grounded in real evidence, never guessed.**

[![CI](https://github.com/jdw112/resale-listing-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/jdw112/resale-listing-ai/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Postgres 16](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![Status](https://img.shields.io/badge/status-proof--of--concept-orange)](#)

</div>

An automated intake service that turns resale product photos into two linked,
priced records — a **Product** (what the item is) and a **Listing** (this physical
unit for sale) — without fabricating anything it can't ground in real evidence.

A submitter uploads photos (plus an optional note and a provided condition grade).
The service identifies the item, prices it against Canadian retail and resale
markets, generates listing copy, cleans up the images, and stores the result in
Postgres. A four-state gate decides whether each submission is **published**,
**rejected**, or sent back to the submitter for **clarification** — a fact only
ever lands in a record when it came from a real source (a barcode, OCR text, a
priced comparable, a cited search result), never from a model guess.

This repository is a **runnable proof** that this (the automated "Option A"
pipeline) is buildable end-to-end. It ships with offline stub adapters so the whole
flow runs with no API keys and no cloud, and with real provider seams you swap in to
go live.

## Contents

- [What's in the box](#whats-in-the-box)
- [Architecture](#architecture)
- [Start the container](#start-the-container)
  - [First admin](#first-admin)
  - [Live processing vs. offline stubs](#live-processing-vs-offline-stubs)
- [Using the service](#using-the-service)
  - [Endpoints at a glance](#endpoints-at-a-glance)
- [Documentation](#documentation)
- [Configuration](#configuration)
- [Running without Docker](#running-without-docker)
- [Contributing](#contributing)
- [License](#license)

## What's in the box

The service is one container image serving three things on the same origin:

- **Submitter portal** — a React SPA: sign up, upload photos, submit, and track each
  submission's status (including answering clarification requests).
- **Intake API** — a FastAPI backend under `/v1/*`: server-to-server submission
  (`X-API-Key`) and the session-cookie routes the portal uses.
- **Interactive API docs** — Swagger UI at `/docs`, ReDoc at `/redoc`, raw OpenAPI at
  `/openapi.json`, all generated from the live app.

Behind the API, submissions are decoupled from processing by two queues (fast
text/pricing stage + heavy image stage), drained by a worker running the same image
with a different command. Postgres is the source of truth.

```
 submitter ──▶ SPA / API ──▶ ingest queue ──▶ worker ──▶ image queue ──▶ worker ──▶ Postgres
 (photos)      (:8000)        (SQS)           (ingest)    (SQS)          (image)     (product +
                                                                                      listing rows)
```

## Architecture

A high-level runtime view: the two ingress paths (browser submitter and
server-to-server `X-API-Key` client), the decoupled two-queue / two-worker pipeline,
its swappable AI provider seams, Postgres and S3, and the trust boundaries that group
them (untrusted clients, the app container, the AWS data plane, third-party egress).

The diagram is an **interactive** page —
dark/light, node focus, relationship tracing, and PNG/SVG export — at
[`docs/resale-listing-ai-runtime-architecture.html`](docs/resale-listing-ai-runtime-architecture.html);
download it or open it locally to explore (GitHub shows the file's source, not the
rendered page). For the written reference, see [`docs/architecture.md`](docs/architecture.md).

## Start the container

Requires Docker with Compose. From the repo root:

```bash
docker compose up --build
```

That brings up the full local stack — **Postgres 16**, **LocalStack** (SQS/S3
emulation, so no AWS account is needed), the **app** (API + frontend on `:8000`), and
the **worker** — all wired together. Nothing external is required.

Once it's up:

| What | URL |
|---|---|
| Submitter portal (SPA) | http://localhost:8000 |
| Interactive API docs (Swagger UI) | http://localhost:8000/docs |
| API reference (ReDoc) | http://localhost:8000/redoc |
| OpenAPI spec (JSON) | http://localhost:8000/openapi.json |

The frontend and API share one origin (no Vite proxy), so there's no cross-origin
cookie/CSRF split. Run detached with `docker compose up --build -d`; stop with
`docker compose down` (add `-v` to also drop the Postgres/uploads volumes).

### First admin

There's no self-service admin signup — `/v1/auth/register` only ever creates a
`submitter`. Bootstrap the first admin from env:

- Set `RESALE_LISTING_AI_ADMIN_EMAIL` + `RESALE_LISTING_AI_ADMIN_PASSWORD` in `.env` (gitignored;
  copy `.env.example`). The `app` service runs with `RESALE_LISTING_AI_BOOTSTRAP_ADMIN=1`, so
  on startup it **creates** that admin (or **promotes** an existing user of that email;
  an existing password is never changed). It's idempotent — a safe no-op on every
  later `up`.
- Or do it by hand any time:

  ```bash
  docker compose exec app python -m resale_listing_ai.admin create-admin you@example.com
  # --password, else $RESALE_LISTING_AI_ADMIN_PASSWORD, else an interactive prompt
  ```

### Live processing vs. offline stubs

Out of the box the `worker` runs **live providers** using the keys in `.env`. To
identify real photos you need provider keys (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`PHOTOROOM_API_KEY`, …); see `.env.example` for the full set and
[`docs/DEVELOPING.md`](docs/DEVELOPING.md) for what each seam calls. To run **offline**
(free, no keys — real submissions get **Rejected** at the gate because the stubs can't
identify real photos), add `RESALE_LISTING_AI_STUBS: "1"` to the `worker` service's
`environment` block in `docker-compose.yml`.

## Using the service

**Through the portal:** open http://localhost:8000, sign up, upload one or more
photos, pick a provided condition grade, and submit. The submission list shows each
one move through `RECEIVED` → processing → its terminal state, and surfaces any
clarification the pipeline asks for.

**Through the API (server-to-server):** every `/v1/*` intake route needs an
`X-API-Key` header — set `RESALE_LISTING_AI_API_KEYS` (comma-separated) in `.env`, then:

```bash
# enqueue a submission (image refs must already be uploaded under RESALE_LISTING_AI_UPLOADS_DIR)
curl -X POST http://localhost:8000/v1/submissions \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"images":["photo1.jpg"],"notes":null,"provided_condition_grade":"Open Box"}'
# -> {"submission_id":"...","status":"RECEIVED"}

# poll status
curl http://localhost:8000/v1/submissions/<submission_id> -H "X-API-Key: $KEY"
```

Set a `callback_url` on the submission to get an http(s) completion POST instead of
polling. The full request/response shape for every endpoint — intake (`X-API-Key`),
auth and submitter (`/v1/auth/*`, `/v1/me/*`, session cookie), and admin
(`/v1/admin/*`) — is in the live **Swagger UI at `/docs`**, with try-it-out.

### Endpoints at a glance

| Group | Auth | What |
|---|---|---|
| `POST /v1/submissions`, `GET /v1/submissions/{id}`, `POST /v1/uploads` | `X-API-Key` | Server-to-server intake + status |
| `/v1/auth/*` (register, login, logout, me) | session cookie | Account + session |
| `/v1/me/*` (submissions, uploads, condition-grades) | session cookie | Submitter's own data (the portal) |
| `/v1/admin/*` (config, submissions) | admin session cookie | Admin config + all submissions |

## Documentation

| Doc | For |
|---|---|
| [User Guide](docs/guides/user-guide.md) | Operators/reviewers: set up, run/monitor/retry/stop, read outputs, update config, costs, CSV→SharePoint |
| [Submitter Guide](docs/guides/submitter-guide.md) | Ops staff using the portal: submit items, read outcomes, answer clarifications |
| [Admin Guide](docs/guides/admin-guide.md) | Admins: create admins, review submissions, tune the gate and providers |
| [API Integrator Guide](docs/guides/api-guide.md) | Server-to-server intake over `/v1/*` with `X-API-Key` |
| [Architecture](docs/architecture.md) | Technical reference: records, pipeline, gate, data/queue planes, invariants |
| [Data Dictionary](docs/data-dictionary.md) | Every Product/Listing field: type, required, provenance, and additions beyond baseline |
| [Requirements Traceability](docs/requirements-traceability.md) | Each PRD v2.2 requirement mapped to code + tests, with honest status |
| [DEVELOPING.md](docs/DEVELOPING.md) | Hands-on internals: offline demos, provider seams, the test suite |

The live, generated API reference is always at `/docs` (Swagger) and `/redoc`.

## Configuration

Real values live in `.env` (never committed — see `.gitignore`); `.env.example`
documents the shape. `docker-compose.yml` overrides the DB/queue/AWS wiring so the
in-container plumbing is always correct regardless of what `.env` targets. Key vars:

| Var | Purpose |
|---|---|
| `RESALE_LISTING_AI_ADMIN_EMAIL` / `RESALE_LISTING_AI_ADMIN_PASSWORD` | Bootstrap admin (see above) |
| `RESALE_LISTING_AI_API_KEYS` | Comma-separated keys for `X-API-Key` intake routes |
| `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `PHOTOROOM_API_KEY`, `CLIPDROP_API_KEY`, … | Live provider keys |
| `RESALE_LISTING_AI_STUBS=1` | Force every adapter to the offline mock path |
| `RESALE_LISTING_AI_COOKIE_SECURE=0` | Local HTTP only — auth cookies default to Secure (HTTPS-only) |
| `RESALE_LISTING_AI_USD_TO_CAD` | Override the USD→CAD factor used to price token usage (default from `config/model_rates.json`) |

Provider selection (which vendor backs each seam) is env-driven too — see
[`docs/DEVELOPING.md`](docs/DEVELOPING.md#selectable-providers).

### Token-based cost

LLM text/vision calls (the OpenAI default stack) are billed from the real
`input`/`output` token counts the provider returns, priced against
[`config/model_rates.json`](config/model_rates.json) (USD per 1M tokens +
a `usd_to_cad` factor) and recorded per row in the cost ledger alongside the
model id. Operations with no tokens (image generation, web search,
deterministic OCR/barcode) and any call on a model absent from the rate table
fall back to a flat per-call unit cost, so a run never breaks on an unpriced
model. Rates are point-in-time — verify them against the live provider pricing
page before go-live. Reporting (`run_findings.py`) breaks tokens down by stage.

## Running without Docker

The pipeline is stdlib-first and runs entirely offline for demos and tests — no
container, no keys:

```bash
python3 run_demo.py         # end-to-end demo -> out/product.csv, listing.csv, cost_log.jsonl
python3 -m pytest -q        # full test suite
```

Both default to an in-memory sqlite store, so there's nothing to set up. For the
real-provider demo (`run_live_demo.py`), the adapter seams, the data and queue
planes, and everything else about extending the code, see
[`docs/DEVELOPING.md`](docs/DEVELOPING.md).

## Contributing

Development setup, the pipeline internals, provider seams, and the test suite are
documented in [`docs/DEVELOPING.md`](docs/DEVELOPING.md). Before opening a PR, run the
full suite and keep it green:

```bash
python3 -m pytest -q
```

## License

Released under the [MIT License](LICENSE).
