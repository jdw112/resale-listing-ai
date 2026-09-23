# User Guide — resale-listing-ai

How to set up, run, operate, and read the enrichment workflow end to end. This is the operator/reviewer
guide the PRD (§15) asks for; for role-specific detail see the companion guides:

- [Submitter Guide](submitter-guide.md) — using the portal to submit and track items
- [Admin Guide](admin-guide.md) — admin console, gate tuning, provider selection
- [API Integrator Guide](api-guide.md) — server-to-server intake over `/v1/*`

Reference docs: [Architecture](../architecture.md) · [Data Dictionary](../data-dictionary.md) ·
[Requirements Traceability](../requirements-traceability.md).

---

## 1. Prerequisites & setup

You need **Docker with Compose**. From the repo root:

```bash
docker compose up --build
```

That brings up the full local stack — **Postgres 16**, **LocalStack** (SQS/S3 emulation, no AWS account
needed), the **app** (API + portal on `:8000`), and the **worker**. Then:

| What | URL |
|---|---|
| Submitter portal | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |

Run detached with `docker compose up --build -d`; stop with `docker compose down` (add `-v` to also drop
the Postgres/uploads volumes).

**First admin** (no self-service admin signup): set `RESALE_LISTING_AI_ADMIN_EMAIL` + `RESALE_LISTING_AI_ADMIN_PASSWORD`
in `.env`, or create one by hand:

```bash
docker compose exec app python -m resale_listing_ai.admin create-admin you@example.com
```

**Live processing vs offline stubs.** Out of the box the worker runs **live** providers using the keys in
`.env` (`OPENAI_API_KEY`, `PHOTOROOM_API_KEY`, plus optional `GEMINI_API_KEY`, `OPENROUTER_API_KEY`). To run
**offline** with no keys, set `RESALE_LISTING_AI_STUBS=1` on the worker — note real photos then get **Rejected** at
the gate because the stubs cannot identify them (offline mode is for wiring/demo, not real intake).

---

## 2. Submitting an item & what evidence helps

**Through the portal:** open http://localhost:8000, sign up, upload one or more photos, pick a provided
condition grade, and submit. The submission list shows each item move `RECEIVED` → processing → its terminal
state, and surfaces any clarification asked for. See the [Submitter Guide](submitter-guide.md).

**Through the API** (needs an `X-API-Key` from `RESALE_LISTING_AI_API_KEYS`):

```bash
curl -X POST http://localhost:8000/v1/submissions \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"images":["photo1.jpg"],"notes":null,"provided_condition_grade":"Open Box"}'
```

**Evidence that improves the result** (the workflow reads only what's visible — it never guesses):

- A readable **UPC/EAN barcode** — the strongest identity anchor.
- A clear **model / rating / serial label** close-up.
- The **product from a few angles**, plus any **packaging/box** and **included accessories**.
- Photos of **damage or wear** so condition is grounded in evidence.
- An optional **note** (e.g. "tested, powers on") — folded into identification and research.

More, clearer evidence ⇒ fewer unknowns and fewer clarification requests. Two weak photos of an unlabeled
item will (correctly) be sent back for clarification or rejected rather than guessed.

---

## 3. Running, monitoring, retrying, stopping

**Run.** With `docker compose up`, the worker drains both queues continuously. For a local one-shot or a
manual daemon:

```bash
docker compose exec app python -m resale_listing_ai.worker --run-once   # drain what's queued once, then exit
docker compose exec app python -m resale_listing_ai.worker --loop --interval 2   # local daemon
```

**Monitor.**
- **Admin Console → Submissions** — every submission, its gate outcome, and per-item cost.
- **Container logs** — `docker compose logs -f worker` shows per-stage progress and any retries.
- **Cost ledger** — every model/search/image call is logged (see §7); the offline demo also writes
  `out/cost_log.jsonl`.

**Retry.** Transient failures are retried automatically with exponential backoff + jitter; a message that
exhausts retries stays for its own visibility-timeout redelivery and eventually lands in the **dead-letter
queue** rather than being lost. Processing is **idempotent** — a redelivered or manually replayed submission
is a no-op, never a duplicate row or double charge — so replaying a stuck item is safe.

**Stop.** `docker compose down` (add `-v` to wipe volumes). Stopping mid-run is safe; in-flight messages
return to the queue and resume on the next start.

---

## 4. Reading the outputs

**Four gate outcomes** (each records its reason):

| Outcome | Meaning | What to do |
|---|---|---|
| **Accepted** | Evidence supports a complete record | Review and approve |
| **Accepted with unknowns** | Useful record; some fields left blank/undetermined | Approve, or add evidence to fill the blanks |
| **Clarification required** | A specific photo/fact would resolve the gap (it names what's missing) | Get that photo/fact and resubmit |
| **Rejected** | No accurate result is possible without material new evidence | Re-shoot or set aside — counted separately, not a failure |

**Unknowns & confidence.** Every field is stored as `{value, confidence, source}`. A blank/unknown field is
`value:null, confidence:0.0, source:"none"` — never a fabricated placeholder. A fact only lands when it has a
real `source` (barcode, label OCR, vision, a cited lookup, …) above the confidence threshold (default 0.60).
So a blank means "not supported by evidence," not "forgotten." Full field-by-field semantics: the
[Data Dictionary](../data-dictionary.md).

**Where records live.** Postgres is the source of truth (two tables, joined by `ProductKey`). Images are
stored under `originals/`, `scrubbed/`, and `finished/` per submission, with URLs on the records. A flat CSV
export is derived from the store:

```bash
python3 run_demo.py    # writes out/product.csv, out/listing.csv, out/cost_log.jsonl
```

The CSV carries the plain `value` per column (stable names); the `confidence`/`source` stay queryable in the
database's JSON columns.

**Condition disagreements.** `ProvidedConditionGrade` (yours) is never overwritten; a differing
`AISuggestedConditionGrade` is kept alongside it and **flagged for review** — a prompt to look, not an error.

---

## 5. Updating prompts, models & schemas safely

Everything below is **configuration** — no code change, no rebuild:

- **Prompts** — `config/prompts/*.json` (`vision_identify`, `retail_price`, `copy`, `hero_generate`,
  `hero_qc`, `image_classify`, …). Edit the instructions/schema; keep the JSON schema intact.
- **Model / provider per seam** — `config/provider_stack.json`, or point a seam at a different vendor from
  **Admin Console → Providers** (no restart). Environment overrides also work:
  `RESALE_LISTING_AI_VISION_PROVIDER`, `RESALE_LISTING_AI_WEB_SEARCH_PROVIDER`, `RESALE_LISTING_AI_COPY_PROVIDER`,
  `RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER`, `RESALE_LISTING_AI_HERO_GENERATE_PROVIDER`. Defaults are **OpenAI**
  (identify/search/copy) and **Photoroom** (image/hero).
- **Schemas** — `config/product_schema.json`, `config/listing_schema.json`. Add a field (name/type/group/
  required) and the DDL, CSV columns, and record objects follow automatically. Document any addition beyond
  the baseline (see the [Data Dictionary](../data-dictionary.md)).
- **Gate thresholds / mandatory fields** — tunable in the Admin Console (`admin_config`).

**After any change, verify before trusting it:**

```bash
python3 -m pytest -q      # full suite
python3 run_demo.py       # end-to-end sanity + fresh CSV/cost outputs
```

Keep the **confidence threshold** meaningful — lowering it to fill more cells trades truthfulness for
coverage, against the project's central standard.

---

## 6. Expected costs

- **Offline stubs:** $0 (no external calls).
- **Live (OpenAI + Photoroom), measured over 17 real folders:** ~**CAD $0.34 per completed listing**; the
  images stage is ~63% of cost. With a **paid** Photoroom key (no sandbox watermark, no wasted hero
  regeneration) this trends toward ~$0.28, against the ~$0.25 target. A reused/deduped product is
  cheaper — it skips re-pricing and re-copy.

Every model/search/image call — including retries and failures — is logged by stage and service, so you can
see exactly where cost goes and compare providers (`run_live_demo.py` prints a per-provider comparison).

---

## 7. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `Could not reach the Postgres server at 'hawkeye'` | `.env` `DATABASE_URL` points at an old host. Use the local stack (compose overrides the DB wiring), or unset `RESALE_LISTING_AI_DB_MODE` to fall back to the offline sqlite store. |
| Every real submission is **Rejected** | Running with `RESALE_LISTING_AI_STUBS=1` (stubs can't identify real photos), or missing provider keys. Add keys to `.env`, unset the stub flag. |
| Generated **website hero** flagged for manual review | The **free/sandbox Photoroom** key stamps a watermark, which hero-QC correctly rejects. The marketplace `HeroImageURL` (real photo) is fine. Use a **paid** Photoroom key to clear it. |
| Port `8000`/`5432`/`4566` already in use | Stop the conflicting service or change the published port in `docker-compose.yml`. |
| Auth cookie not set on local HTTP | Set `RESALE_LISTING_AI_COOKIE_SECURE=0` for local HTTP (cookies default to HTTPS-only). |
| A submission looks stuck | Safe to replay — processing is idempotent. Check `docker compose logs -f worker` and the DLQ. |

---

## 8. Preparing CSV for SharePoint / Power Automate

The output is **channel-neutral** and built to remap later:

1. Export: `python3 run_demo.py` → `out/product.csv` (57+ columns) and `out/listing.csv` (33 columns).
2. **Stable columns.** Column names/types/order are generated from the schemas and don't drift — map them
   once into your SharePoint list columns.
3. **Join key.** `ProductKey` links Listing → Product (many Listings per Product). Preserve it as the
   relationship key when importing.
4. **Blanks are meaningful.** An empty cell = unknown/unsupported, not missing data — keep it empty rather
   than filling a default.
5. **Provenance** (`confidence`/`source`) is not in the CSV; it lives in the Postgres JSON columns. If a later
   workflow needs it, export it from the store rather than expecting it in the flat CSV.
6. **Images** are referenced by URL (originals/scrubbed/finished); repoint those to your own storage when you
   migrate off LocalStack/S3.

The `HeroImageURL` is the actual-item marketplace main image; the separate AI `WebsiteHeroImageURL` (and its
`WebsiteHeroReviewRequired` flag) are for a website context — see the [Data Dictionary](../data-dictionary.md).
