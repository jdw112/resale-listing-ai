# Requirements Traceability Matrix

Maps every requirement in the **PRD (`AI-Assisted Product Data Enrichment Workflow`, v2.2)**
and the **Data Contract (v1.0)** to where it is implemented in this repository and the
evidence that it works.

| | |
|---|---|
| **Requirement baseline** | PRD v2.2 (AI-Assisted Product Data Enrichment Workflow) · Data Contract v1.0 |
| **Code revision** | `b39ac14` (branch `main`) |
| **Verification method** | Static inspection of the code against each requirement, cross-referenced to the existing test suite (**566 test functions** across `tests/`). |
| **Status date** | 2026-09-02 |

### Status legend

| Status | Meaning |
|---|---|
| ✅ **Met** | Implemented on the real path and exercised by tests (on a live and/or mock/stub fixture). |
| 🟡 **Partial** | Core behaviour present, but a named part of the requirement is unimplemented or only works under a constraint (noted). |
| ⚪ **Stub-only** | Wired end-to-end but real output requires a live provider key; offline stubs cannot satisfy it. |
| ❌ **Not-met** | Not implemented. |

> **Honesty note.** "Met" means the *mechanism* exists and is tested. It does **not** claim measured
> model accuracy on real photographs. Identification-accuracy figures are produced by `resale_listing_ai/findings.py`
> against **mock/known-answer** fixtures; a genuine accuracy/hallucination rate needs a labelled real-photo
> answer key (see [Known gaps](#known-gaps--caveats) and the separate test-results report).

---

## Scoreboard

| | Must | Should | Total |
|---|---|---|---|
| ✅ Met | 18 | 2 | **20** |
| 🟡 Partial | 4 | 0 | **4** |
| ❌ Not-met | 0 | 1 | **1** |
| **Functional requirements** | 22 | 3 | **27** (FR-1…27) |

Non-functional: 8 Met, 2 Partial. Nine outcome areas (§6.1): all covered. Appendix C must-not-miss: 9 Met, 1 Partial, 1 Not-met.

---

## Functional requirements (§12)

| FR | Pri | Requirement (abbreviated — see PRD §12 for full text) | Status | Where | Evidence |
|---|---|---|---|---|---|
| FR-1 | Must | Accept a submission for one unit: 2–10+ images, optional notes, provided condition grade | ✅ | `resale_listing_ai/api.py`, `resale_listing_ai/pipeline.py` | `test_api.py`, `test_uploads.py`, `test_me_submissions.py` |
| FR-2 | Must | Assess sufficiency **before** identify/generate → Accepted / Accepted-with-unknowns / Clarification / Rejected, with reason | ✅ | `pipeline.gate()` | `test_pipeline.py` |
| FR-3 | Must | On clarify/reject, name what is missing | ✅ | `pipeline.gate()` missing-list, `api.py` clarification route | `test_pipeline.py`, `test_api.py` |
| FR-4 | Must | Preserve all originals unaltered in a separate original-photo folder | ✅ | `adapters` `originals/{sid}/` vs `scrubbed/{sid}/` | `test_adapters.py`, `test_image_path_resolve.py` |
| FR-5 | Must | Decode UPC/EAN; OCR model/MPN/brand/serial/rating-label text | ✅ (live) | `adapters.barcode_read` (zbar), `adapters.ocr_read` (Tesseract) | `test_adapters.py` — deterministic; offline stubs return fixtures |
| FR-6 | Must | Identify to brand/model/variant/colour/generation; no cross-variant/region merge | ⚪→✅ | `adapters.vision_identify` | `test_adapters.py`, `test_field_enrichment.py` — real output needs a vision key; offline stub cannot identify real photos (→ Rejected) |
| FR-7 | Must | One reasonably-specific `InternalCategory` + `InternalSubcategory`, open vocabulary | ✅ | vision map + `config/categories.json` | `test_field_enrichment.py` |
| FR-8 | Must | Incorporate the optional notes field into identification/research | ✅ | `adapters._instructions_with_ocr`, notes passed to vision/pricing | `test_adapters.py` |
| FR-9 | Must | Populate the Product baseline (Appendix A): identity, classification, physical, specs, measurements, contents, copy, assets, notes | 🟡 | `records.blank_product`, `config/product_schema.json`, `adapters` | Core fields populated & tested; **packaged measurements and manual/installation URLs have no intake adapter** (see FR-10, FR-12) |
| FR-10 | Must | Derive `OversizedFlag` from packaged size/weight vs a documented UPS Canada baseline; Unable-to-Determine when unknown | 🟡 | `helpers.oversized_flag`, `config/oversized_ups_ca.json` | `test_helpers.py` — logic correct and tested, **but there is no intake path for packaged dimensions**, so end-to-end it resolves to *Unable to Determine* |
| FR-11 | Must | Generate reusable copy: MasterTitle, ShortTitle, MasterDescription, 5 bullets, SearchTerms, SEOKeywords — from verified facts only | ✅ | `adapters.generate_copy`, `config/prompts/` | `test_field_enrichment.py`, `test_adapters.py` |
| FR-12 | Should | Capture `ManualURL` / `InstallationGuideURL` (EN/FR), retain `ProductSpecificationsSourceURL` | ❌ | schema fields exist in `config/product_schema.json` | **No adapter populates them** ("gap #5", noted in `pipeline.py`). Spec-source URL only partially available |
| FR-13 | Must | Populate the Listing baseline (Appendix B) and link via `ProductKey` | ✅ | `records.blank_listing`, `pipeline`, `db` | `test_db.py`, `test_pipeline.py` |
| FR-14 | Must | Randomized, non-sequential SKU; extract serial separately; never invent a serial | ✅ | `helpers.make_sku`; serial via `ocr_read` | `test_helpers.py`, `test_adapters.py` |
| FR-15 | Must | Record `ProvidedConditionGrade` (controlled) **and** `AISuggestedConditionGrade`; flag on disagreement; never silently override | ✅ | `pipeline` + `adapters`; condition flag in `findings` | `test_pipeline.py`, `test_field_enrichment.py` |
| FR-16 | Must | Completeness: included/missing items & components; `OriginalPackagingIncluded` and `OriginalBoxIncluded` as **distinct** fields | ✅ | `config/listing_schema.json` | `test_field_enrichment.py` |
| FR-17 | Must | `MissingHardware` = Yes/No/Unable; defects, functional concerns, testing status/tests/results | ✅ | `config/listing_schema.json` | `test_field_enrichment.py` |
| FR-18 | Must | Select customer-facing vs evidence-only vs redundant photos; document why 2 vs 3 supporting images | 🟡 | `adapters.image_process` classification | Selection implemented & tested; **per-image "why 2 vs 3" rationale is not captured as a structured field** |
| FR-19 | Must | Produce a 3–4 image finished set (crop, background, lighting) without hiding condition | ✅ | `image_scrub.py`, `adapters.image_process` | `test_image_scrub.py` |
| FR-20 | Must | Create **or** select one accurate product-level hero; verify accuracy; define fallback; store `HeroImageURL` in Product | ✅ | `adapters` hero (Tier-1 scrubbed real photo; Tier-3 AI generate), `config/prompts/hero_generate` | `test_prompts_hero.py` — **Tier-2 (retrieve an authoritative manufacturer image) is not implemented** |
| FR-21 | Must | Store 2–3 supporting-image URLs in Listing; set availability flags; leave unused URL columns blank | ✅ | `config/listing_schema.json` (`ListingImageURL1..8`, `*PhotosAvailable`) | `test_field_enrichment.py` |
| FR-22 | Must | `PricingAnchorPriceCAD` + primary source/URL, ≥1 corroborating source, a comparable when no exact match, confidence + check date, flag >10% conflicts | ✅ | `adapters.retail_price` / `resale_price` mapping | `test_adapters.py`, `test_findings.py` — the >10% disagreement **lowers `PricingConfidence` and is surfaced in findings** rather than a dedicated boolean field |
| FR-23 | Must | Separate machine-readable Product & Listing outputs (Excel/CSV) linked by `ProductKey`; preserve blank/unknown/confidence/exception | ✅ | `records.write_csv`, `db.export_csv`; envelope in JSON columns | `test_db.py` |
| FR-24 | Must | Host original/scrubbed/finished images + expose URLs; keep prompts, model settings, rules, schemas configurable | ✅ | S3/local storage; `config/provider_stack.json`, `config/prompts/`, `admin_config` | `test_bootstrap_s3.py`, `test_admin_config.py` |
| FR-25 | Must | Per-stage, per-model cost for every call/search/image op incl. retries/failures; avg per attempt and per completed listing | ✅ | `cost.CostLedger` (`stage`, `service`, `retry`), `by_stage` | `test_worker.py`, `test_findings.py` |
| FR-26 | Must | Retain source URLs/references for key facts; leave unsupported fields blank/undetermined rather than fabricate | ✅ | `{value, confidence, source}` envelope; gate threshold | `test_pipeline.py`, `test_field_enrichment.py` |
| FR-27 | Should | Support reprocessing and batch runs across supplied sets | ✅ | `findings.py` / `run_findings.py` batch; idempotent worker replay | `test_findings.py`, `test_findings_metrics.py`, `test_worker.py` |

---

## Non-functional requirements (§13)

| Requirement | Status | Where / note |
|---|---|---|
| **Truthfulness** — no fabricated facts/images/serials/prices; expose uncertainty; reject when unsupported | ✅ | Envelope + gate + never-invent rules (`envelope.py`, `pipeline.gate`, `helpers`) |
| **Accuracy target** (~90/10 mix; ~1–2% intervention) — PoC measures actual, not claims target | 🟡 | `findings.compute_findings` reports the §4.2 measures **separately**; accuracy is measured on **mock/known-answer** fixtures, not a labelled real-photo corpus |
| **Processing cost** target ~CAD $0.25/completed listing (target, not cap) | ✅ | `cost.CostLedger` captures actuals; per-provider unit costs in `adapters` / `providers/*`; compared in `run_live_demo.py` |
| **Cost capture** — every op incl. retries/failures, by stage/service, avg per attempt & per listing, **fixed subscriptions separate from variable** | 🟡 | Variable per-op cost fully captured and tested; **fixed-subscription vs variable split is a reporting convention, not a ledger field** |
| **Data design** — separate, linked, stable, remappable records; CSV acceptable | ✅ | Two schemas in `config/*_schema.json`; Postgres source of truth; CSV a derived export |
| **Configurability** — prompts, models, rules, schemas configurable without rebuild | ✅ | `config/provider_stack.json`, `config/prompts/`, `admin_config` overrides |
| **Privacy** — product evidence/UPCs only; no customer/personal data | ✅ | No PII collected in intake; submitter accounts are auth-only |
| **Portability** — output remappable to SharePoint/Power Automate later | ✅ | Stable CSV columns, channel-neutral schema |
| **Latency/throughput** — measured & reported, not gated | ✅ | `findings.process_sample` measures ingest/image wall time |

---

## Nine outcome areas (§6.1)

| # | Outcome area | Covered by | Status |
|---|---|---|---|
| 1 | Intake validation | FR-1, FR-2, FR-3 | ✅ |
| 2 | Photo selection & processing | FR-4, FR-18, FR-19 | ✅ (FR-18 rationale 🟡) |
| 3 | Reusable hero image | FR-20 | ✅ (Tier-2 not built) |
| 4 | Product Information | FR-9, FR-11, FR-12 | 🟡 (FR-12 not built, packaged measures gap) |
| 5 | Listing Information | FR-13–FR-17, FR-21 | ✅ |
| 6 | Market pricing research | FR-22 | ✅ |
| 7 | Structured workflow output | FR-23, FR-24 | ✅ |
| 8 | Model choice & processing cost | FR-25 + NFR cost | ✅ |
| 9 | Testing & evaluation | FR-27, `findings.py` | ✅ (accuracy on mocks — see caveats) |

---

## Appendix C — must-not-miss checklist

| Item | Status | Reference |
|---|---|---|
| Condition grading: provided value **and** evidence-based AI suggestion | ✅ | FR-15 |
| `OriginalPackagingIncluded` and `OriginalBoxIncluded` as distinct fields | ✅ | FR-16 |
| `MissingHardware` with an Unable-to-Determine state | ✅ | FR-17 |
| Separate generated SKU and extracted serial number | ✅ | FR-14 |
| Product-level hero + unit-specific supporting images | ✅ | FR-20, FR-21 |
| Product and packaged measurements + `OversizedFlag` (UPS Canada baseline) | 🟡 | FR-10 — packaged measurements have no intake path |
| Manual and installation-guide capture (EN/FR) | ❌ | FR-12 — not implemented |
| Market pricing anchor only — not listing/selling price | ✅ | FR-22 (and scope §6.2) |
| Insufficient-evidence rejection & clarification, counted separately | ✅ | FR-2 + `findings` separate counts |
| Measured processing cost without sacrificing quality | ✅ | FR-25 / NFR cost |
| Working implementation assets | ✅ | this repository |

---

## Known gaps & caveats

Consolidated, so they are visible in one place (each is honest scope, not a hidden defect):

1. **Packaged measurements have no intake path** → `OversizedFlag` resolves to *Unable to Determine* end-to-end (FR-10, App C #6).
2. **`ManualURL` / `InstallationGuideURL` are not populated** — schema fields exist, no adapter ("gap #5") (FR-12, App C #7).
3. **Reused-product "unknowns" inheritance** — a deduped Product inherits whatever core-field gaps its *first* unit left; later units of that product report "Accepted with unknowns" for a reason that is not about that unit, and there is no per-later-unit retry path (documented in `pipeline.py`).
4. **Offline stubs cannot identify real photos** — with `RESALE_LISTING_AI_STUBS=1`, real submissions are **Rejected** at the gate. Live identification/pricing/copy/image paths require provider keys (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `PHOTOROOM_API_KEY`, …).
5. **Identification accuracy is measured on mock/known-answer fixtures.** `findings.py` measures pipeline correctness (gate branching, dedupe, cost, idempotency) and identification accuracy against a fixture answer key — **not** a labelled real-photo corpus. A genuine accuracy/hallucination rate needs such a corpus.
6. **Hero Tier-2 not implemented** — Tier-1 (scrubbed real photo) and Tier-3 (AI generation, accuracy-verified) exist; retrieving a verified authoritative manufacturer image is not built (FR-20).
7. **FR-18 image-selection rationale** is applied but not captured as a structured "why 2 vs 3" field.
8. **Cost fixed-vs-variable split** is a reporting convention over the ledger, not a ledger field (NFR cost capture).

---

## How to re-verify

- Run the suite: `python3 -m pytest -q` (in-memory sqlite + local queue; no keys needed).
- Batch the §4.2 measures over sample sets: `python3 run_findings.py` → identification/completeness/cost/exception findings.
- Per-provider cost/quality comparison (needs keys): `python3 run_live_demo.py`.

This matrix is **derived from the PRD and Data Contract as supplied**; confirm the requirement baseline
before treating it as sign-off. It records implementation status, not a measured accuracy
result — the latter belongs in the Comprehensive Report's test-results section (PRD §15).
