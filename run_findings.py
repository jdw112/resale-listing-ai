"""Batch runner over samples/ -> the PRD §4.2 measures (identification accuracy,
per-stage cost/time, manual-intervention rate, exceptions/rejections reported
SEPARATELY from completed listings) and the findings log.

    python3 run_findings.py           # mock + hard-case samples (always, offline)
                                       # + a stub-mode dry run over any downloaded
                                       # real "Product NN" folders (harness
                                       # validation only -- see the printed caveat)
    python3 run_findings.py --live    # use REAL adapters for the Product NN
                                       # folders. Needs OPENAI_API_KEY /
                                       # PHOTOROOM_API_KEY / etc. actually set AND
                                       # authorized for this use -- neither is true
                                       # by default, so this is an explicit opt-in,
                                       # never silently triggered.

Mock/hard-case samples always run with RESALE_LISTING_AI_STUBS=1 (that's how the
mock_evidence.json -> adapters._KNOWN injection works — see resale_listing_ai/findings.py);
--live only affects the separate Product NN pass, run after the mock pass so the
two never interfere with each other's adapter mode.
"""

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("RESALE_LISTING_AI_STUBS", "1")  # default: offline, same as run_demo.py

from resale_listing_ai import adapters
from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.findings import (
    EXPECTED_STATUS_BY_SAMPLE, compute_findings, discover_mock_submissions,
    discover_real_product_folders, load_mock_submission, mock_evidence_to_known, process_sample,
)

OUT = Path(__file__).resolve().parent / "out"


def run_mock_and_hard_case_samples(store, ledger):
    os.environ["RESALE_LISTING_AI_STUBS"] = "1"
    records = []
    for folder in discover_mock_submissions():
        submission, hint, evidence = load_mock_submission(folder)
        adapters._KNOWN[hint] = mock_evidence_to_known(evidence)
        outcome = process_sample(submission, store, ledger, set())
        outcome.update(
            submission_id=submission["submission_id"], hint=hint,
            expected_status=EXPECTED_STATUS_BY_SAMPLE.get(hint),
            cost_rows=store.cost_rows(submission["submission_id"]),
        )
        records.append(outcome)
    return records


def run_real_product_folders(store, ledger, live=False):
    os.environ["RESALE_LISTING_AI_STUBS"] = "0" if live else "1"
    records = []
    for folder in discover_real_product_folders():
        submission_id = folder.name.replace(" ", "_")
        images = sorted(str(p) for p in folder.glob("*.jpg"))
        submission = {"submission_id": submission_id, "images": images, "notes": None,
                      "provided_condition_grade": "Used - Good"}
        outcome = process_sample(submission, store, ledger, set())
        outcome.update(
            submission_id=submission_id, hint=folder.name,
            expected_status=None,  # Notes.txt is a narrative, not a structured answer key
            cost_rows=store.cost_rows(submission_id),
        )
        records.append(outcome)
    os.environ["RESALE_LISTING_AI_STUBS"] = "1"
    return records


def _print_findings(title, findings):
    print(f"\n--- {title} ---")
    print(f"  Total submissions        : {findings['total_submissions']}")
    print(f"  Completed listings       : {findings['completed_count']}")
    print(f"  Exceptions/rejections    : {findings['exceptions_count']}  "
          f"(reported separately, per PRD §4.2) -> {findings['exceptions_by_status']}")
    print(f"  Avg cost / completed ($) : {findings['avg_cost_per_completed_listing_cad']}  "
          f"(target <= ${findings['cost_target_cad']}; meets target: {findings['meets_cost_target']})")
    print(f"  Cost by stage ($)        : {findings['cost_by_stage_cad']}")
    print(f"  Tokens by stage          : {findings['tokens_by_stage']}")
    print(f"  Total tokens             : in={findings['total_input_tokens']} "
          f"out={findings['total_output_tokens']}")
    print(f"  Avg time (s): ingest={findings['avg_ingest_seconds']} "
          f"images={findings['avg_images_seconds']} total={findings['avg_total_seconds']}")
    print(f"  Manual intervention      : {findings['manual_intervention_count']}/"
          f"{findings['total_submissions']} ({findings['manual_intervention_rate']:.0%})")
    pc = findings["pipeline_correctness"]
    if pc["tested"]:
        print(f"  Pipeline correctness     : {pc['matched_expected']}/{pc['tested']} "
              f"({pc['accuracy']:.0%}) matched the independently-reasoned expected gate outcome")


def main():
    parser = argparse.ArgumentParser(description="Batch runner over samples/ -> PRD §4.2 findings")
    parser.add_argument("--live", action="store_true",
                         help="use real adapters for the Product NN folders (needs authorized API keys)")
    args = parser.parse_args()

    store = Store.connect()
    store.ensure_schema()
    ledger = CostLedger()

    mock_records = run_mock_and_hard_case_samples(store, ledger)
    real_records = run_real_product_folders(store, ledger, live=args.live)

    mock_findings = compute_findings(mock_records)
    _print_findings("Mock + hard-case samples (pipeline-correctness harness — no live model in the loop)",
                     mock_findings)

    if real_records:
        real_findings = compute_findings(real_records)
        label = ("Real product photos — LIVE adapters" if args.live else
                 "Real product photos — STUB-MODE DRY RUN (harness validation only; "
                 "no live vision model, so 0% recognized is expected, not a real accuracy figure)")
        _print_findings(label, real_findings)
    else:
        real_findings = None
        print("\n--- Real product photos ---\n  none downloaded to samples/Product NN/ in this checkout "
              "(git-ignored sample material — see samples/README.md)")

    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "mock_and_hard_case": {"findings": mock_findings,
                                "records": [{"submission_id": r["submission_id"], "hint": r["hint"],
                                             "status": r["result"]["status"], "expected_status": r["expected_status"],
                                             "cost_cad": r["result"].get("cost_cad")} for r in mock_records]},
        "real_product_photos": None if real_findings is None else {
            "live": args.live, "findings": real_findings,
            "records": [{"submission_id": r["submission_id"], "hint": r["hint"],
                         "status": r["result"]["status"], "cost_cad": r["result"].get("cost_cad")}
                        for r in real_records]},
    }
    (OUT / "findings.json").write_text(json.dumps(report, indent=2))
    print(f"\nFull report written to {OUT / 'findings.json'}")
    store.close()


if __name__ == "__main__":
    main()
