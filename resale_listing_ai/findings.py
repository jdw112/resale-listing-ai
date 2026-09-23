"""Batch runner over samples/ — PRD §4.2 ("Success Measures, reported separately")
+ the Accuracy & Findings Log.

Two very different kinds of sample, deliberately not conflated into one score:

  Mock submissions (samples/000*, git-tracked) — a submission.json + a
  mock_evidence.json standing in for "what a real vision/barcode/pricing stack
  would have returned" (per samples/README.md). Since there's no live model in
  the loop for these, they measure PIPELINE correctness — gate branching, dedupe,
  cost, idempotency — not model accuracy. mock_evidence_to_known() converts one
  into the same adapters._KNOWN[hint] shape the existing offline stub adapters
  already read (identity/measures/pricing/unit/barcode), so a mock submission
  runs through the REAL, unmodified pipeline code, just fed pre-baked "vision
  output" instead of calling a real model.

  Real product folders (samples/Product NN/, git-ignored, present only once
  downloaded) — real photos + a Notes.txt narrative
  answer key. Genuine identification-accuracy/hallucination measurement needs a
  real vision call per photo (OPENAI_API_KEY etc.) that this environment doesn't
  have authorization to use yet; discover_real_product_folders() finds them so
  the harness is ready the moment real keys are configured, and returns an empty
  list (not an error) when they're absent, e.g. on a fresh clone.
"""

import json
import time
from collections import Counter
from datetime import date
from pathlib import Path

from . import pipeline

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

_PRICE_DISAGREEMENT_THRESHOLD = 0.10  # matches retail_price's own "~10%" documented rule

# Expected gate outcome per mock sample, reasoned independently from the gate's
# documented rules (envelope.THRESHOLD=0.60, "a single clear miss is usually
# resolvable -> clarification; else reject" — pipeline.gate) rather than copied
# from whatever the code happens to output — this is what makes
# pipeline_correctness a real regression check (tests/test_findings_samples.py
# asserts actual output matches) rather than a tautology.
#
# NOTE (2026-08-06): pipeline._has_no_unknowns now distinguishes a bare
# "Accepted" from "Accepted with unknowns" for real (see CORE_FIELDS in
# pipeline.py) -- previously every completed listing hardcoded the latter.
# These samples' status expectations were re-verified empirically against
# regenerated samples/ + tests/test_findings_samples.py (not just guessed
# from their comments) and all remain correct as-is: each "Accepted with
# unknowns" entry genuinely has one or more CORE_FIELDS gaps under the new
# check. The inline comments below name the exact CORE_FIELDS gap(s) each
# sample's mock evidence leaves un-confident (confirmed by inspecting the
# actual product/listing records tests/test_findings_samples.py produces),
# not just a restatement of what the sample is designed to exercise. Also
# note: since six CORE_FIELDS live on the Product record (only computed for a
# NEW product), any of these samples that instead processes as a REUSED
# product -- e.g. under run_findings.py's shared store, unlike this test
# file's per-sample isolated store -- inherits whatever CORE_FIELDS gaps the
# first unit of that product left, which can change its status from what's
# recorded here.
EXPECTED_STATUS_BY_SAMPLE = {
    # DefectsOrDamage is the only CORE_FIELDS gap: mock_evidence.json's
    # listing_fields never includes it (all other CORE_FIELDS -- including
    # PricingAnchorPriceCAD, via two exact_match pricing comps -- are
    # confident). PackagedLengthCM/etc. being unknown does NOT matter here:
    # packaged/unpackaged measurements aren't in CORE_FIELDS at all.
    "000412_nest_doorbell": "Accepted with unknowns",
    # Second unit of the same product (ProductKey-dedupe scenario); under
    # test_findings_samples.py's per-test isolated store it's processed as a
    # standalone NEW product, not a dedupe onto 000412. Three CORE_FIELDS
    # gaps: PricingAnchorPriceCAD (pricing_comps is empty -> no anchor at
    # all), and ActualIncludedItems/DefectsOrDamage (both absent from
    # mock_evidence.json's listing_fields).
    "000501_nest_doorbell_unit2": "Accepted with unknowns",
    "000502_unknown_model": "Rejected",                      # only Brand known -> too little for one question
    "000503_unidentifiable": "Rejected",                     # nothing at all
    # Provided vs. AI condition grade genuinely differ (the sample's design
    # purpose -- record both, raise a review flag, never overwrite the
    # provided value); separately, ActualIncludedItems and DefectsOrDamage
    # are absent from mock_evidence.json's listing_fields, which is the
    # actual CORE_FIELDS gap keeping this out of a bare "Accepted".
    "000504_condition_mismatch": "Accepted with unknowns",
    "000505_weak_confidence_labels": "Rejected",              # all fields present but <0.60 -> same as missing
    # ModelNumber alone (no UPC) still satisfies gate()'s has_anchor check,
    # so this clears the mandatory gate. But mock_evidence.json's
    # product/listing fields are deliberately minimal: pricing_comps is
    # empty and only AISuggestedConditionGrade is set in listing_fields, so
    # PricingAnchorPriceCAD/ActualIncludedItems/OriginalPackagingIncluded/
    # MissingHardware/DefectsOrDamage are all unfilled CORE_FIELDS gaps.
    "000506_no_upc_confident_model": "Accepted with unknowns",
    # Confidently identified (Brand/ProductName/InternalCategory/ModelNumber
    # all >=0.6) despite being a bare disassembled part, and every listing
    # CORE_FIELDS entry (ActualIncludedItems, MissingHardware,
    # OriginalPackagingIncluded, DefectsOrDamage, AISuggestedConditionGrade)
    # is confidently filled too. The ONLY CORE_FIELDS gap is
    # PricingAnchorPriceCAD: pricing_comps is empty, so no anchor price.
    "000507_disassembled_part": "Accepted with unknowns",
    # The two pricing comps disagreeing by a wide margin does NOT by itself
    # lower PricingAnchorPriceCAD's own confidence in the current code --
    # _convert_pricing_comps sets the anchor's confidence from exact_match
    # alone (0.8 here), and the disagreement only lowers the separate,
    # informational PricingConfidence label to "Low". The real CORE_FIELDS
    # gaps are that mock_evidence.json's listing_fields sets only
    # AISuggestedConditionGrade, leaving ActualIncludedItems,
    # OriginalPackagingIncluded, MissingHardware, and DefectsOrDamage unset.
    "000508_conflicting_price_sources": "Accepted with unknowns",
}


# ---------------------------------------------------------------------------
# mock_evidence.json -> adapters._KNOWN[hint] shape
# ---------------------------------------------------------------------------

def _convert_pricing_comps(comps, *, today_iso=None):
    """[{price_cad, source_name, source_url, exact_match}, ...] -> the
    PricingAnchor*/CorroboratingSource*/PricingConfidence envelope fields.
    Two sources disagreeing by more than ~10% -> Low confidence, surfaced, never
    silently averaged or silently trusted."""
    if not comps:
        return {}
    today_iso = today_iso or date.today().isoformat()
    anchor = comps[0]
    anchor_conf = 0.8 if anchor.get("exact_match") else 0.6
    out = {
        "PricingAnchorPriceCAD": {"value": anchor["price_cad"], "confidence": anchor_conf, "source": "external_lookup"},
        "PricingAnchorSourceName": {"value": anchor["source_name"], "confidence": anchor_conf, "source": "external_lookup"},
        "PricingAnchorSourceURL": {"value": anchor["source_url"], "confidence": anchor_conf, "source": "external_lookup"},
        "LastPricingCheckDate": {"value": today_iso, "confidence": 1.0, "source": "derived"},
    }
    if len(comps) > 1:
        corroborating = comps[1]
        out["CorroboratingSourcePriceCAD"] = {
            "value": corroborating["price_cad"], "confidence": 0.7, "source": "external_lookup"}
        out["CorroboratingSourceName"] = {
            "value": corroborating["source_name"], "confidence": 0.7, "source": "external_lookup"}
        out["CorroboratingSourceURL"] = {
            "value": corroborating["source_url"], "confidence": 0.7, "source": "external_lookup"}
        hi, lo = max(anchor["price_cad"], corroborating["price_cad"]), min(anchor["price_cad"], corroborating["price_cad"])
        disagreement = (hi - lo) / hi if hi else 0.0
        confidence_label = "Low" if disagreement > _PRICE_DISAGREEMENT_THRESHOLD else "High"
    else:
        confidence_label = "Medium"
    out["PricingConfidence"] = {"value": confidence_label, "confidence": 0.8, "source": "derived"}
    return out


def mock_evidence_to_known(evidence):
    """mock_evidence.json's {barcodes, product_fields, listing_fields,
    pricing_comps} -> adapters._KNOWN[hint]'s {barcode, identity, measures,
    pricing, unit}. product_fields/listing_fields already use the real Product/
    Listing schema field names and the real {value,confidence,source} envelope
    shape (see samples/_build_samples.py), so most of this is a direct pass-
    through — only pricing_comps needs real conversion."""
    barcodes = evidence.get("barcodes") or []
    return {
        "barcode": barcodes[0] if barcodes else None,
        "identity": dict(evidence.get("product_fields") or {}),
        "measures": {},
        "pricing": _convert_pricing_comps(evidence.get("pricing_comps") or []),
        "unit": dict(evidence.get("listing_fields") or {}),
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_mock_submissions(samples_dir=SAMPLES_DIR):
    """Folders with BOTH submission.json and mock_evidence.json — deterministic,
    git-tracked, always present."""
    samples_dir = Path(samples_dir)
    if not samples_dir.exists():
        return []
    return sorted(
        d for d in samples_dir.iterdir()
        if d.is_dir() and (d / "submission.json").exists() and (d / "mock_evidence.json").exists()
    )


def discover_real_product_folders(samples_dir=SAMPLES_DIR):
    """"Product NN" folders with real photos — git-ignored sample material,
    present only once downloaded (samples/README.md). Empty (not an error) on a
    fresh clone."""
    samples_dir = Path(samples_dir)
    if not samples_dir.exists():
        return []
    return sorted(
        d for d in samples_dir.iterdir()
        if d.is_dir() and d.name.startswith("Product ") and any(d.glob("*.jpg"))
    )


def load_mock_submission(folder):
    """-> (submission dict with absolute image paths + hint=folder name, hint,
    raw evidence dict)."""
    folder = Path(folder)
    submission = dict(json.loads((folder / "submission.json").read_text()))
    evidence = json.loads((folder / "mock_evidence.json").read_text())
    submission["images"] = [str(folder / img) for img in submission.get("images", [])]
    hint = folder.name
    submission["hint"] = hint
    return submission, hint, evidence


# ---------------------------------------------------------------------------
# Running one submission with stage-level wall-clock timing (PRD §4.2:
# "Processing time: Total and stage-level")
# ---------------------------------------------------------------------------

def process_sample(submission, store, ledger, skus):
    """Runs pipeline._stage_ingest / _stage_images directly (rather than
    pipeline.run()) so ingest-phase and images-phase wall time can be measured
    separately. Idempotent the same way run() is (store.get_submission_result
    checked first)."""
    sid = submission["submission_id"]
    cached = store.get_submission_result(sid)
    if cached is not None:
        return {"result": cached, "ingest_seconds": 0.0, "images_seconds": 0.0, "total_seconds": 0.0}

    t0 = time.perf_counter()
    terminal, payload = pipeline._stage_ingest(submission, store, ledger, skus)
    ingest_seconds = time.perf_counter() - t0

    if terminal:
        pipeline._flush_cost_rows(store, ledger, sid)
        store.record_submission(sid, payload["status"], payload)
        return {"result": payload, "ingest_seconds": ingest_seconds, "images_seconds": 0.0,
                "total_seconds": ingest_seconds}

    t1 = time.perf_counter()
    result = pipeline._stage_images(
        submission, payload["product_key"], payload["sku"], payload["reused"], store, ledger)
    images_seconds = time.perf_counter() - t1

    pipeline._flush_cost_rows(store, ledger, sid)
    store.record_submission(sid, result["status"], result)
    return {"result": result, "ingest_seconds": ingest_seconds, "images_seconds": images_seconds,
            "total_seconds": ingest_seconds + images_seconds}


# ---------------------------------------------------------------------------
# compute_findings — PRD §4.2 measures, reported SEPARATELY (never one rolled-up
# success rate: completed listings, exceptions/rejections, cost, time, and
# manual-intervention rate are each their own number).
# ---------------------------------------------------------------------------

COST_TARGET_CAD = 0.25


def _avg(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def _needs_manual_intervention(record):
    """Clarification required always does (that's the point of the state); an
    Accepted listing where the provided condition grade and the AI-suggested one
    disagree also does — PRD: 'Provided vs AI condition are separate columns
    with a condition_flag so disagreements surface without overwriting the
    employer's value,' and a surfaced disagreement is exactly the kind of thing
    a reviewer needs to look at."""
    result = record["result"]
    if result["status"] == "Clarification required":
        return True
    listing = result.get("listing")
    if not listing:
        return False
    provided = listing.get("ProvidedConditionGrade", {}).get("value")
    ai_suggested = listing.get("AISuggestedConditionGrade", {}).get("value")
    return bool(provided and ai_suggested and provided != ai_suggested)


def compute_findings(records):
    """records: [{"result": <pipeline result dict>, "cost_rows": [...],
    "ingest_seconds", "images_seconds", "total_seconds", "expected_status"
    (optional, mock/hard-case only)}, ...] -> the PRD §4.2 measures. Pure, no I/O."""
    completed = [r for r in records if r["result"]["status"].startswith("Accepted")]
    exceptions = [r for r in records if not r["result"]["status"].startswith("Accepted")]

    completed_costs = [r["result"].get("cost_cad") or 0.0 for r in completed]
    avg_cost_per_completed = _avg(completed_costs)

    cost_by_stage = {}
    tokens_by_stage = {}
    for r in records:
        for row in r.get("cost_rows", []):
            cost_by_stage[row["stage"]] = round(cost_by_stage.get(row["stage"], 0.0) + row["cost_cad"], 4)
            if row.get("input_tokens") is None and row.get("output_tokens") is None:
                continue
            agg = tokens_by_stage.setdefault(row["stage"], {"input_tokens": 0, "output_tokens": 0})
            agg["input_tokens"] += row.get("input_tokens") or 0
            agg["output_tokens"] += row.get("output_tokens") or 0
    total_input_tokens = sum(a["input_tokens"] for a in tokens_by_stage.values())
    total_output_tokens = sum(a["output_tokens"] for a in tokens_by_stage.values())

    manual_intervention = [r for r in records if _needs_manual_intervention(r)]

    with_expectation = [r for r in records if r.get("expected_status") is not None]
    matched_expected = [r for r in with_expectation if r["result"]["status"] == r["expected_status"]]

    return {
        "total_submissions": len(records),
        "completed_count": len(completed),
        "exceptions_count": len(exceptions),
        "exceptions_by_status": dict(Counter(r["result"]["status"] for r in exceptions)),
        "avg_cost_per_completed_listing_cad": avg_cost_per_completed,
        "cost_target_cad": COST_TARGET_CAD,
        "meets_cost_target": (
            None if avg_cost_per_completed is None else avg_cost_per_completed <= COST_TARGET_CAD),
        "cost_by_stage_cad": cost_by_stage,
        "tokens_by_stage": tokens_by_stage,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "avg_ingest_seconds": _avg(r["ingest_seconds"] for r in records),
        "avg_images_seconds": _avg(r["images_seconds"] for r in records),
        "avg_total_seconds": _avg(r["total_seconds"] for r in records),
        "manual_intervention_count": len(manual_intervention),
        "manual_intervention_rate": round(len(manual_intervention) / len(records), 4) if records else 0.0,
        "pipeline_correctness": {
            "tested": len(with_expectation),
            "matched_expected": len(matched_expected),
            "accuracy": round(len(matched_expected) / len(with_expectation), 4) if with_expectation else None,
        },
    }
