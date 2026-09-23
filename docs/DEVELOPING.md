# Developing resale-listing-ai

Internals, the offline demos, the provider seams, and the data/queue planes. For
what the service is and how to start the container, see the top-level
[`README.md`](../README.md); this document is for extending the code.

## Run the offline demos (stdlib only)

```bash
python3 run_demo.py         # end-to-end demo -> writes out/product.csv, listing.csv, cost_log.jsonl
python3 run_findings.py     # batch runner over ../samples/ -> PRD §4.2 measures, out/findings.json
python3 -m pytest -q        # full test suite covering the non-negotiable rules
```

All three default to the offline sqlite-backed store (`RESALE_LISTING_AI_DB_MODE=sqlite`, stdlib
`sqlite3`, no setup) — see **Postgres as the source of truth** below for running against
a real server instead.

### `run_live_demo.py` — the real-provider demo

```bash
RESALE_LISTING_AI_VISION_PROVIDER=gemini RESALE_LISTING_AI_COPY_PROVIDER=gemini \
RESALE_LISTING_AI_WEB_SEARCH_PROVIDER=gemini_search \
python3 run_live_demo.py uploads/photo1.jpg uploads/photo2.jpg
```

Unlike `run_demo.py` (stubs, no keys), this makes **real provider calls** on 1–2 real
product photos and prints the extracted identity, pricing, generated copy and a
per-provider cost breakdown — run it once per provider set and compare the reports. It
calls `pipeline._stage_ingest` only, deliberately stopping before `_stage_images` (which
unconditionally needs PhotoRoom/Clipdrop/S3 keys this repo doesn't carry), so **no image
processing happens**. Needs `GEMINI_API_KEY` and/or `OPENROUTER_API_KEY` (see
`.env.example`) plus system `zbar` for `barcode_read` and `tesseract` for `ocr_read`.

Note that `_stage_ingest` dedupes by `ProductKey`: run the same photos twice against a
*persistent* store and the second run reuses the first product, so pricing/copy are not
regenerated. The script prints a loud warning when that happens, since it would otherwise
show the first provider's output under the second provider's banner. (The default store is
in-memory sqlite, which starts empty each run — this only bites with
`RESALE_LISTING_AI_SQLITE_PATH` or Postgres set.)

## What the demo proves

| Behaviour | How it shows up |
|---|---|
| Two linked records | `out/product.csv` (58 cols) + `out/listing.csv` (33 cols), joined by `ProductKey` |
| Four-state gate | submission 3 (unidentifiable) → **Rejected**, with named missing fields; no record fabricated |
| No hallucination | rejected item produces no product/listing; serial/price only ever from a real `source` |
| ProductKey dedupe | two units of the same product → **1 Product row, 2 Listing rows** |
| Provided condition immutable | `ProvidedConditionGrade` kept verbatim, `source:"provided"`, confidence 1.0 |
| OversizedFlag | derived from packaged dims vs UPS-CA config; **Unable to Determine** when dims unknown |
| Cost tracking | per-stage ledger + avg cost per completed listing (target ~CAD $0.25) |
| Grounded copy | `MasterTitle` etc. `source:"generated"`, written only from `confident()` Product facts — never a fact the model wasn't shown |
| Repair never fabricates | `json_repair.py`'s schema-coercion pass can only null a field or drop an out-of-schema one; it cannot add a value not present in its input |
| Postgres dedupe | `product` row upserted with `ON CONFLICT ("ProductKey") DO UPDATE` — a second unit of the same product reuses the row, never a duplicate |
| Idempotent redelivery | re-running the same `submission_id` returns the identical cached result — no re-run stage, no duplicate `cost_ledger`/listing row (see the demo's "Redelivery check" line) |

## Layout

```
resale_listing_ai/
  envelope.py     {value, confidence, source} + the 0.60 gate helper
  records.py      loads config/*_schema.json -> the 58/33 field records (never hardcoded)
  helpers.py      OversizedFlag (UPS-CA) + randomized SKU
  cost.py         cost ledger (per-run buffer; Store.record_cost_rows persists it)
  json_repair.py  schema-coercion + LLM-fallback repair, used at each stage boundary
  db.py           Store — Postgres (or offline sqlite) source of truth, table DDL
                  generated from config/*_schema.json, ON CONFLICT upsert + submission
                  idempotency
  adapters.py     provider seams (real, with RESALE_LISTING_AI_STUBS=1 offline fallback)
  providers/      alternative provider modules behind those seams:
                  gemini.py (google-genai), openrouter.py (openai SDK + base_url)
  pipeline.py     stages + four-state gate; _stage_ingest/_stage_images + run()
                  (the synchronous composition of both, dedupe via Store)
  queue.py        ingest-queue/image-queue + DLQs — real SQS or offline LocalQueue
  worker.py       queue consumers wrapping the two pipeline stages, retry+backoff,
                  --run-once CLI
  api.py          intake API (FastAPI): POST /v1/submissions, GET /v1/submissions/{id},
                  POST /v1/uploads (direct file upload) — these routes require X-API-Key;
                  /v1/auth/* (below) uses session-cookie auth instead; /v1/me/* (uploads,
                  submissions, condition-grades) also uses session-cookie auth, scoped to
                  the logged-in submitter's own data
  auth.py         password hashing (bcrypt) + session/CSRF token primitives for
                  /v1/auth/* (register/login/logout/me) — pure, no FastAPI coupling
run_demo.py       3-submission end-to-end demo (stubs, no keys)
run_live_demo.py  real-provider 1-2 image demo (_stage_ingest only, no image stage)
tests/            rule-based tests (test_db_live.py needs a real Postgres, skipped by
                  default; test_queue_sqs.py uses moto — real boto3 SQS calls, no
                  Docker needed)
config/           the schemas + rules plus
                  provider_stack.json (real-provider selection) and prompts/ (the 7
                  model prompts adapters.py loads at import -- edit these .json files
                  to tune a prompt, no code change needed)
frontend/         React + TypeScript SPA (Vite) — login/signup, the submitter portal
                  (/submit, /submissions, /submissions/:id) over session-cookie auth;
                  see frontend/README.md for how to run it
Dockerfile          single image (multi-stage): builds the SPA, serves it + the
                    FastAPI backend with uvicorn — see the README's container section
docker-compose.yml  local stack: Postgres 16 + LocalStack (SQS/S3) + app + worker
```

## Going live (Option A)

Each function in `adapters.py` is a seam. Keep the signatures; swap the stub body for a
real call:

| Adapter | Status | Real provider |
|---|---|---|
| `vision_identify` | **Real** | OpenAI Vision (Responses API, structured JSON) by default; **Gemini** or **OpenRouter** selectable — see *Selectable providers* below |
| `barcode_read` | **Real** | pyzbar/zbar (deterministic, local, no cost); needs system `zbar` (`brew install zbar`) |
| `ocr_read` | **Real** | pytesseract/Tesseract (deterministic, local, no cost); feeds `vision_identify` as context, never fills a field directly; needs system `tesseract` (`brew install tesseract`) |
| `image_match` | **Real** | web-search-with-citations provider (below); confirms identity when there's no confident UPC |
| `retail_price` | **Real** | same provider; NEW-market Canadian retail → `PricingAnchorPriceCAD` + retained source URLs |
| `resale_price` | **Real** | same provider; used/resale context → `Comparable*` fields only, never the anchor |
| `image_process` | **Real** | per-image classify + clutter bbox (OpenAI Vision by default; **Gemini**/**OpenRouter** selectable) -> PhotoRoom (background removal) -> Clipdrop Cleanup (masked inpaint, flagged images only) -> S3; needs `PHOTOROOM_API_KEY`, `CLIPDROP_API_KEY` |
| `generate_copy` (copy stage) | **Real** | OpenAI by default, **Gemini**/**OpenRouter** selectable; given ONLY `confident()` Product facts |

### Selectable providers

Four seams accept an alternative provider, each chosen by its own env var (defaults in
`config/provider_stack.json`, unchanged: OpenAI everywhere):

| Env var | Seams it controls | Values |
|---|---|---|
| `RESALE_LISTING_AI_VISION_PROVIDER` | `vision_identify` **and** `image_process`'s classify step (same kind of call: images in, structured JSON out) | `openai` (default), `gemini`, `openrouter` |
| `RESALE_LISTING_AI_COPY_PROVIDER` | `generate_copy` | `openai` (default), `gemini`, `openrouter` |
| `RESALE_LISTING_AI_WEB_SEARCH_PROVIDER` | `image_match` / `retail_price` / `resale_price` (one provider backs all three) | `openai_web_search` (default), `perplexity_sonar`, `gemini_search`, `openrouter_online` |
| `RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER` | `image_process`'s background-removal/inpaint step | `photoroom_clipdrop` (default), `gemini` |

Keys: `GEMINI_API_KEY` for Gemini, `OPENROUTER_API_KEY` for OpenRouter, `PERPLEXITY_API_KEY`
for Perplexity (see `.env.example`). OpenRouter additionally needs
`RESALE_LISTING_AI_OPENROUTER_MODEL` — it has **no default**, because OpenRouter routes to many
vendors and its catalog changes over time; set it to a model id from your OpenRouter
dashboard (e.g. `google/gemini-2.0-flash-001`). The provider modules live in
`resale_listing_ai/providers/{gemini,openrouter}.py`; OpenRouter reuses the `openai` package with a
different `base_url` (the same trick Perplexity uses), so it adds no new dependency.

OpenAI and Perplexity are given the target JSON schema **structurally**, via their APIs'
`json_schema` response format. Gemini and OpenRouter are called with a loose JSON response
mode instead — Gemini's `google_search` grounding tool can't be combined with structured
output in the same call — so `adapters.py` appends the serialized schema to the prompt text
for those two. Either way the response goes through `json_repair` before it's trusted.

Each call (including retries and failures) logs a `cost_ledger` row when a `ledger` is
passed in, tagged with the provider-specific service that actually ran (`gemini_vision`,
`openrouter_copy`, `openai_web_search`, …) and that provider's unit cost — so a cost
comparison between providers reflects what each one really charges.

Pricing runs against the **confirmed post-gate identity**: `_stage_ingest` calls
`retail_price(submission, identity, ledger=…)` and `resale_price(submission, identity,
ledger=…)` directly. (This was previously a documented gap — a `pricing_lookup()` helper
called only `resale_price(submission, None)`, never `retail_price`, so live pricing output
was always empty. That helper is gone; `tests/test_pipeline.py` now pins the real identity
being passed to both.)

### JSON repair, at each stage boundary

`resale_listing_ai/json_repair.py` sits between every real adapter's raw model output and the
`_map_*_output` function that turns it into envelopes (`vision_identify`, `image_match`,
`retail_price`, `resale_price`, `image_process`'s classify step, and `generate_copy` all go
through it). Two passes:

1. **Local schema coercion** (free, no network) — parse the raw text, then project it onto
   the adapter's own JSON schema: anything missing, mistyped, or not an allowed enum value
   becomes `null`; anything not declared in the schema is dropped. This is the fast path for
   every well-formed response (OpenAI's `strict` JSON-schema mode already returns clean
   output, so this is normally the only pass that runs) and it's what makes "never
   introduces a value" true in code, not just in a prompt.
2. **LLM fallback** — only when the raw text isn't valid JSON at all (prose, a broken
   fragment). Provider is selectable in `config/provider_stack.json`'s `json_repair_provider`
   (default `claude`, needs `ANTHROPIC_API_KEY`; `openai` reuses the existing OpenAI key) —
   override at runtime with `RESALE_LISTING_AI_JSON_REPAIR_PROVIDER`. Whatever the model returns is
   run back through the same local coercion before being trusted, so a fabricated extra
   field never survives either.

Set `RESALE_LISTING_AI_STUBS=1` to force every adapter — including the real ones above — back to
the offline mock path. `run_demo.py` sets this itself, so the demo still needs no API
keys/network/zbar. `pytest` does the same for `tests/test_pipeline.py`, which drives the
pipeline through the mock `"hint"` data; `tests/test_adapters.py` exercises the real
adapters directly (pyzbar always; OpenAI, Perplexity, Gemini and OpenRouter via mocked
clients, since this repo carries no live keys), and `tests/providers/` covers the two new
provider modules the same way.

`pipeline.run(submission, store, ledger, skus)` now takes a `Store` (`resale_listing_ai/db.py`)
instead of an in-memory dict — see **Postgres as the source of truth** below.
`pipeline.run` is wrapped in a queue/worker runtime behind an intake API — see
**Queue, worker, and intake API** further down.

## Postgres as the source of truth

"Postgres is the source of truth, not CSV" (System Design §6.1/§11).
`resale_listing_ai/db.py`'s `Store` replaces the old in-memory `product_index` dict entirely —
`pipeline.run()` dedupes and persists through it, never a plain dict.

**Two modes, switched by `RESALE_LISTING_AI_DB_MODE`:**

| Mode | When | Needs |
|---|---|---|
| `sqlite` (default) | `pytest`, `run_demo.py` — offline, dependency-free, always green | nothing — stdlib `sqlite3` |
| `postgres` | live/demo/production | `DATABASE_URL` + `psycopg[binary]` + network to the server |

Put real values in `.env` (never committed — see `.gitignore`; `.env.example` shows the
shape). `Store.connect()` loads `.env` automatically (a tiny stdlib-only reader — real
exported env vars always win) and picks the mode from `RESALE_LISTING_AI_DB_MODE`. For a local
Postgres: `docker compose up -d postgres`, then
`DATABASE_URL=postgresql://resale_listing_ai:resale_listing_ai@localhost:5432/resale_listing_ai`.

**Tables** (DDL generated from `config/product_schema.json`/`listing_schema.json` — see
`build_schema_sql`, never hand-maintained):
- `product` — one row per `ProductKey` (PK), upserted with `ON CONFLICT ("ProductKey")
  DO UPDATE` — this is the dedupe mechanism a second unit of the same product reuses.
- `listing` — one row per physical unit, PK `SKU`.
- `submission` — PK `submission_id`; the idempotency ledger. `pipeline.run()` checks this
  FIRST, before any stage runs — a cache hit returns the exact prior result verbatim, so
  redelivery/reprocessing (e.g. an at-least-once SQS retry) is a safe no-op: no re-run
  stage, no new SKU, no duplicate `cost_ledger` row. Also carries `user_id`/
  `parent_submission_id` (null for `X-API-Key` submissions) — a submitter portal
  submission's owner and, for a clarification-answer resubmission, the thread's root.
- `cost_ledger` — append-only mirror of `CostLedger.rows`, flushed once per submission
  (only on first-time processing, never on a cached replay).

Every `product`/`listing` row stores each schema field as its own flattened value column
plus one `field_meta` JSON column holding `{field: {confidence, source}}` for all of
them — so `product.csv`/`listing.csv` (`Store.export_csv`) are a genuinely **derived**
export (flattened values only, no `field_meta`), never the working store.

`tests/test_db.py` covers all of this offline against sqlite (including a real `ON
CONFLICT DO UPDATE` conflict — sqlite has supported it since 3.24, so this is a real SQL
engine's upsert semantics, not a Python re-implementation of them). `tests/test_db_live.py`
runs the same checks (plus a full `pipeline.run()` dedupe + reprocessing round trip)
against a real Postgres server; it's skipped unless `RESALE_LISTING_AI_DB_MODE=postgres` is set,
and cleans up every row it writes (`DELETE`, never `DROP`) so it's safe to run repeatedly
against a shared server.

## Queue, worker, and intake API

System Design §3/§5/§8/§9: intake is decoupled from processing by two SQS queues (one
for the fast text/pricing stages, one for the heavy image stage), each with its own
dead-letter queue, drained by workers that retry transient failures with backoff+jitter
and are safe under at-least-once redelivery.

**Try it locally (no AWS, no Docker):**

```bash
python3 -c "
import os
os.environ['RESALE_LISTING_AI_API_KEYS'] = 'demo-key'

from resale_listing_ai.db import Store
from resale_listing_ai.queue import connect_queues
from resale_listing_ai.api import create_app
from resale_listing_ai import worker
from resale_listing_ai.cost import CostLedger
from fastapi.testclient import TestClient

store = Store.connect(); store.ensure_schema()
queues = connect_queues()
client = TestClient(create_app(store=store, queues=queues))
headers = {'X-API-Key': 'demo-key'}

created = client.post('/v1/submissions', headers=headers, json={
    'images': ['a.jpg'], 'notes': None, 'provided_condition_grade': 'Open Box',
    'hint': 'nest-doorbell'}).json()   # 'hint' is demo-only, for the offline stub adapters
print(created)                         # {'submission_id': '...', 'status': 'RECEIVED'}
print(client.get(f\"/v1/submissions/{created['submission_id']}\", headers=headers).json())  # still RECEIVED

worker.run_once(store, queues, ledger=CostLedger())   # drains ingest-queue, then image-queue

print(client.get(f\"/v1/submissions/{created['submission_id']}\", headers=headers).json())  # now PUBLISHED
"
```

Or run a worker as its own process: `python -m resale_listing_ai.worker --run-once` drains
whatever's currently queued and exits — a long-running poll loop is a thin wrapper
around the same `run_once`/`run_ingest_worker_once`/`run_image_worker_once` functions.

**Components:**

| Component | Role | Realization here |
|---|---|---|
| Intake API | `POST /v1/submissions` enqueues + returns a `submission_id`; `GET /v1/submissions/{id}` polls status; `POST /v1/uploads` accepts files directly; these routes require an `X-API-Key` header, and `callback_url` (if set) gets an http(s)-only completion POST. `/v1/auth/*` (register/login/logout/me) and `/v1/me/*` (uploads, submissions, condition-grades) use session-cookie auth instead — no `X-API-Key` | `resale_listing_ai/api.py`, FastAPI |
| Ingest queue + DLQ | Buffers submissions; decouples intake bursts from workers | `resale_listing_ai/queue.py`, `connect_queues()["ingest"/"ingest_dlq"]` |
| Image queue + DLQ | Fans the heavy image stage out separately (System Design §7: "isolates the heavy/variable stage so text extraction stays responsive") | `connect_queues()["image"/"image_dlq"]` |
| Ingest worker | Runs `pipeline._stage_ingest` (identify→gate→product/listing) | `worker.run_ingest_worker_once` |
| Image worker | Runs `pipeline._stage_images` (image_process→finalize) | `worker.run_image_worker_once` |

**Two queue modes**, same `RESALE_LISTING_AI_*_MODE` pattern as the store:

| Mode | When | Needs |
|---|---|---|
| `local` (default) | `pytest`, `run_demo.py`, `--run-once` demo — offline, always green | nothing — pure in-memory, real visibility-timeout/receive-count/DLQ-redrive semantics, just no network |
| `sqs` | live/production | real Amazon SQS, or LocalStack (`docker compose up -d localstack`, then `AWS_ENDPOINT_URL=http://localhost:4566`) via the identical boto3 calls |

**Per-stage idempotency**, not just whole-submission: `submission.state` (`db.py`) tracks
`RECEIVED` (API accepted it, no worker has touched it yet) → `INGESTED` (ingest phase
done, images pending) → `DONE` (terminal — published, or rejected/clarification-required
at the gate, which skips the images phase entirely). Both workers check this *before*
doing any work, so a message redelivered mid-flight — after ingest completed but before
the image job ran, or after everything completed — is a no-op: no re-minted SKU, no
duplicate listing row, no duplicate `cost_ledger` rows. (`tests/test_worker.py` covers
exactly this — a submission fully processed, then redelivered, produces byte-identical
results and zero new rows.)

**Retry**: transient failures are retried with exponential backoff + jitter (bounded
attempts), each attempt logged to `cost_ledger` under `service="worker"` — System Design
§9: *"Retry with exponential backoff + jitter; each retry logged to the cost ledger."*
Exhausting retries leaves the message **undeleted** in its queue rather than swallowing
the failure — the queue's own visibility timeout redelivers it, and after
`max_receive_count` attempts it's redriven to the DLQ automatically (proven for real SQS
semantics via moto in `tests/test_queue_sqs.py`, no Docker needed).

**Visibility timeout** defaults to 300s (`queue.DEFAULT_VISIBILITY_TIMEOUT`) — comfortably
above image processing's realistic p95 (a handful of seconds per image × a few images),
per §8: *"Set above the slowest stage's p95."* A production deployment with genuinely
long image jobs would heartbeat-extend visibility rather than raise this further; that
heartbeat isn't implemented here.

**Known simplifications** (disclosed rather than silently overreached):
- The intake API only accepts already-uploaded image *references* (local paths in this
  scaffold; S3 keys in a real deployment) — per §5.1, large images go to S3 via
  presigned URLs client-side, so the API never proxies file bytes. `image_process` itself
  still only reads local paths, though (it doesn't fetch-by-S3-key), so real S3-key input
  isn't fully wired end-to-end yet. Because those local paths are read straight off the
  filesystem (`Path(path).read_bytes()`) and then forwarded to a third-party image API,
  `POST /v1/submissions` rejects (422) any image reference that isn't a relative path
  resolving inside `RESALE_LISTING_AI_UPLOADS_DIR` (default `starter_repo/uploads/`) — otherwise a
  submission could make a worker read and exfiltrate an arbitrary server file (`.env`, SSH
  keys, etc). This containment check goes away once real S3-key fetch replaces local-path
  reads.
- The ingest-queue message carries the full submission body inline rather than the
  doc's lighter `{submission_id, s3_prefix, ...}` reference (which assumes a separate
  fetch-by-id step this scaffold doesn't have yet).
- If a worker crashes after committing `_stage_ingest` but before the image-queue `send()`
  call completes, that submission is stuck at `INGESTED` with no image job in flight —
  a real deployment would want a periodic sweep (or an outbox pattern) to catch this; not
  implemented here.

## Notes
- Stub data is illustrative (both demo units share a serial because they share one mock
  source); real runs read each unit's own evidence.
- Confidence values in the stubs are placeholders; the 0.60 gate threshold is set in
  `envelope.py` and should be tuned on real sample data.
