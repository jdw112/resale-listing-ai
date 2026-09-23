# Architecture

A technical reference for how resale-listing-ai is put together: the record model,
the pipeline and its gate, the data and queue planes, the provider seams, and the
invariants that hold the whole thing to "never fabricate." For hands-on internals
(running the demos, swapping providers, the test suite) see
[`DEVELOPING.md`](DEVELOPING.md).

> Proof-of-concept. This documents what the scaffold actually does today, including
> the simplifications it makes on purpose ([Known simplifications](#known-simplifications)).

## Purpose

Turn resale product photos into two linked, priced records without inventing
anything that can't be grounded in real evidence. It's a runnable proof that the
automated ("Option A") intake pipeline is buildable end to end — offline stub
adapters let the whole flow run with no keys and no cloud, and real provider seams
swap in to go live.

## The record model

One submission is one physical unit. Processing produces up to two records:

- **Product** — reusable make/model/variant facts, keyed by `ProductKey` (58
  fields: identity, physical, measures, listing copy, pricing anchor, assets). One
  Product backs many units.
- **Listing** — one physical unit for sale, keyed by `SKU` (33 fields: this unit's
  condition, its own photos, its link back to a Product).

Two units of the same product ⇒ **one Product row, two Listing rows**. Dedupe is by
`ProductKey`, enforced in the database (`ON CONFLICT ("ProductKey") DO UPDATE`), not
in Python.

Both schemas are declared as data in [`config/product_schema.json`](../config/product_schema.json)
and [`config/listing_schema.json`](../config/listing_schema.json). The table DDL,
the CSV export columns, and the record objects are all generated from those files —
field lists are never hardcoded.

### The value envelope

Every extracted field is stored as `{value, confidence, source}`, where `source` is
one of `barcode | label_ocr | vision | external_lookup | context | provided |
measured | derived`. This envelope is the backbone of the no-fabrication guarantee:
a fact carries where it came from and how sure the system is, and downstream steps
(the gate, copy generation) only ever act on facts above a confidence threshold.

## The pipeline

`pipeline.run()` composes two stages; the four-state gate sits between them.

```
photos ─▶ _stage_ingest ────────────────▶ gate ─▶ _stage_images ─▶ Product + Listing
          identify · barcode · OCR              (accept path only)   finalize
          image-match · price · copy
```

### Stage 1 — ingest

Identify the item and assemble its facts:

- **vision_identify** — structured identity from the photos (brand, model, colour,
  specs). Reads only what's visible; unsupported fields return `null`, never a
  plausible guess.
- **barcode_read** (zbar) and **ocr_read** (Tesseract) — deterministic, local,
  free. OCR feeds vision as context; it never fills a field directly.
- **image_match** — web search with citations; confirms identity when there's no
  confident UPC.
- **retail_price** / **resale_price** — Canadian new-market retail sets the
  `PricingAnchor*`; used/resale context only ever fills the `Comparable*` fields,
  never the anchor. Both run against the confirmed post-gate identity.
- **generate_copy** — listing title, description, bullets, SEO — written from
  **only** the confident Product facts the model is shown, never a fact it wasn't.

### The four-state gate

`gate()` decides each submission's outcome from the identity's confident facts
(default threshold `0.60`, tunable in the Admin Console):

| Condition | Outcome |
|---|---|
| All mandatory facts present + a confident anchor (UPC or model number) | **Accepted** — or **Accepted with unknowns** if a core field is still blank |
| One or two mandatory facts missing | **Clarification required** — those exact fields are asked back |
| More than two missing | **Rejected** — no record created |

A rejected or clarification-pending submission produces no Product/Listing. The
thresholds, mandatory fields, and core fields are admin-overridable config, checked
via `admin_config.get()`.

### Stage 2 — images

Runs only on the accept path: per-image classify + clutter detection → background
removal → masked cleanup (flagged images only) → storage, then finalize the two
records. This is the heavy, variable-latency stage, which is why it's queued
separately from ingest.

### JSON repair at every boundary

Every real adapter's raw model output passes through
[`json_repair.py`](../resale_listing_ai/json_repair.py) before it's trusted. Two passes:
(1) **local schema coercion** — project the output onto the adapter's JSON schema;
anything missing/mistyped/out-of-enum becomes `null`, anything undeclared is
dropped; (2) **LLM fallback** — only when the raw text isn't valid JSON at all, and
its result is run back through the same coercion. The coercion pass is what makes
"never introduces a value" true in code: it can null or drop a field, never add one.

## Data plane — Postgres is the source of truth

`db.py`'s `Store` is the single persistence layer; there is no in-memory product
index. Two modes, switched by `RESALE_LISTING_AI_DB_MODE`:

- `sqlite` (default) — offline, stdlib `sqlite3`, used by tests and the demos.
- `postgres` — live; needs `DATABASE_URL` + `psycopg`.

Four tables, all DDL generated from the schema configs:

- **`product`** — one row per `ProductKey` (the upsert/dedupe point).
- **`listing`** — one row per unit, PK `SKU`.
- **`submission`** — PK `submission_id`; the **idempotency ledger**. `run()` checks
  it *first*, so redelivery returns the prior result verbatim — no re-run, no new
  SKU, no duplicate cost rows. Also tracks per-stage state (`RECEIVED → INGESTED →
  DONE`) and the owning user / clarification-thread parent.
- **`cost_ledger`** — append-only cost mirror, flushed once per first-time
  processing.

Each Product/Listing row stores every field as its own value column plus one
`field_meta` JSON column holding `{field: {confidence, source}}`. The CSV exports
are a genuinely *derived* view (values only), never the working store.

## Queue / worker plane

Intake is decoupled from processing by two queues, each with a dead-letter queue:
an **ingest queue** (fast text/pricing stage) and an **image queue** (heavy image
stage), isolating the slow stage so identification stays responsive. Modes switch on
`RESALE_LISTING_AI_QUEUE_MODE`:

- `local` (default) — in-memory, with real visibility-timeout / receive-count / DLQ
  semantics; no network. Used by tests and the demos.
- `sqs` — real Amazon SQS, or LocalStack via identical boto3 calls.

Workers (`worker.py`) drain each queue, wrapping the two pipeline stages. They:

- **check per-stage state before doing work**, so a message redelivered mid-flight
  is a no-op (at-least-once safe);
- **retry transient failures** with exponential backoff + jitter, each attempt
  logged to the cost ledger;
- **leave a message undeleted** on exhausted retries, so the queue's visibility
  timeout redrives it and, past `max_receive_count`, moves it to the DLQ.

Default visibility timeout is 300s — comfortably above the slowest stage's p95.

## API & auth

One FastAPI app (`api.py`), served with the built SPA on the same origin, exposes
two auth models:

- **Intake** (`/v1/submissions`, `/v1/uploads`) — `X-API-Key`, server-to-server.
- **Portal** (`/v1/auth/*`, `/v1/me/*`, `/v1/admin/*`) — session cookie + CSRF
  double-submit; `/v1/admin/*` additionally requires the `admin` role.

Admin is never self-service: registration only creates a `submitter`; the first
admin is bootstrapped from env or the `resale_listing_ai.admin` CLI. See the
[API Guide](guides/api-guide.md) and [Admin Guide](guides/admin-guide.md).

## Provider seams

Each function in `adapters.py` is a seam with a stub body (`RESALE_LISTING_AI_STUBS=1`) and
a real body. Five selectors accept an alternative vendor — the four adapter seams
below plus the JSON-repair fallback — each settable by env var or by the Admin
Console's Providers tab (defaults in
[`config/provider_stack.json`](../config/provider_stack.json)):

| Selector | Seam(s) | Options |
|---|---|---|
| `RESALE_LISTING_AI_VISION_PROVIDER` | vision_identify + image classify | openai · gemini · openrouter · bedrock |
| `RESALE_LISTING_AI_COPY_PROVIDER` | generate_copy | openai · gemini · openrouter |
| `RESALE_LISTING_AI_WEB_SEARCH_PROVIDER` | image_match / retail / resale | openai_web_search · perplexity_sonar · gemini_search · openrouter_online |
| `RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER` | background removal / inpaint | photoroom_clipdrop · gemini · bedrock |
| `RESALE_LISTING_AI_JSON_REPAIR_PROVIDER` | json_repair LLM fallback | claude · openai |

Each call logs a cost-ledger row tagged with the vendor-specific service that
actually ran, so a provider cost comparison reflects real charges. Full seam table
and key requirements: [`DEVELOPING.md`](DEVELOPING.md#going-live-option-a).

## Invariants

The properties the design exists to guarantee — each pinned by tests:

- **No fabrication.** A field is populated only from a real `source`. Repair can
  null or drop, never add. Rejected items produce no record.
- **Provided condition is immutable.** A submitter's grade is kept verbatim
  (`source: "provided"`, confidence 1.0); an AI-suggested grade is retained
  separately, never overwritten.
- **Grounded copy.** Listing copy is generated only from confident Product facts
  the model was shown.
- **Idempotent.** Redelivery/reprocessing of a `submission_id` is a byte-identical
  no-op.
- **Dedupe in the database.** Same product ⇒ one Product row, via SQL upsert.

## Known simplifications

Disclosed rather than silently overreached (full detail in `DEVELOPING.md`):

- Image input is **local path references**, not S3 keys end to end; the image stage
  reads local paths only. A path-containment check (`422` outside the uploads root)
  guards against file exfiltration until real S3-key fetch replaces it.
- The ingest-queue message carries the **full submission inline** rather than a
  light id+prefix reference.
- A crash **after ingest commit but before the image-queue send** strands a
  submission at `INGESTED`; a real deployment would add a sweep/outbox to recover it.
- Long image jobs would **heartbeat-extend visibility** rather than raise the
  timeout; that heartbeat isn't implemented.
- Stub confidence values and the `0.60` threshold are **placeholders to tune** on
  real sample data.

## Map

| Concern | Where |
|---|---|
| Record schemas | `config/{product,listing}_schema.json` |
| Envelope + gate threshold | `resale_listing_ai/envelope.py` |
| Pipeline stages + four-state gate | `resale_listing_ai/pipeline.py` |
| Provider seams | `resale_listing_ai/adapters.py`, `resale_listing_ai/providers/` |
| Persistence | `resale_listing_ai/db.py` |
| Queues + workers | `resale_listing_ai/queue.py`, `resale_listing_ai/worker.py` |
| API + auth | `resale_listing_ai/api.py`, `resale_listing_ai/auth.py` |
| Admin config | `resale_listing_ai/admin_config.py`, `resale_listing_ai/admin.py` |
