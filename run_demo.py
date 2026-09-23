"""End-to-end demonstration of the Option A pipeline.

Runs three submissions through the full stage pipeline with STUB adapters (no API
keys) against the offline sqlite-backed Store (no Postgres/network needed — see
resale_listing_ai/db.py; set RESALE_LISTING_AI_DB_MODE=postgres + DATABASE_URL to point this at a
real server instead), then writes Product/Listing CSVs (a DERIVED export straight
off the store) and a cost log. Demonstrates:
  1. a recognized unit  -> Accepted, complete records
  2. a second unit of the SAME product -> Product row reused (dedupe), new Listing
  3. an unidentifiable item -> Rejected (no fabricated data)
"""

import os
from pathlib import Path

os.environ.setdefault("RESALE_LISTING_AI_STUBS", "1")  # this demo is stub-only, no API keys

from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.pipeline import run
from resale_listing_ai.records import LISTING_FIELDS, PRODUCT_FIELDS, flatten

OUT = Path(__file__).resolve().parent / "out"

SUBMISSIONS = [
    {"submission_id": "2026-07-24-0001", "hint": "nest-doorbell",
     "images": ["img1.jpg", "img2.jpg", "img3.jpg"], "notes": None,
     "provided_condition_grade": "Open Box"},
    {"submission_id": "2026-07-24-0002", "hint": "nest-doorbell",
     "images": ["a.jpg", "b.jpg"], "notes": None,
     "provided_condition_grade": "Used - Good"},
    {"submission_id": "2026-07-24-0003", "hint": "unknown-hvac-part",
     "images": ["p1.jpg"], "notes": None,
     "provided_condition_grade": "Used - Fair"},
]


def main():
    store = Store.connect()
    store.ensure_schema()
    skus, ledger = set(), CostLedger()
    results = [run(s, store, ledger, skus) for s in SUBMISSIONS]

    listings = [r["listing"] for r in results if r["status"].startswith("Accepted")]

    store.export_csv("product", PRODUCT_FIELDS, OUT / "product.csv")
    store.export_csv("listing", LISTING_FIELDS, OUT / "listing.csv")
    ledger.dump(OUT / "cost_log.jsonl")

    print("=" * 68)
    print(f"RESALE LISTING AI ENRICHMENT PIPELINE — Option A demo ({store.dialect} store, stub adapters)")
    print("=" * 68)
    for r in results:
        line = f"[{r['submission_id']}]  {r['status']:<24}"
        if r["status"].startswith("Accepted"):
            dedupe = "  (reused product)" if r.get("reused_product") else "  (new product)"
            line += f"key={r['product_key']:<26} sku={r['listing_sku']}{dedupe}"
        else:
            line += f"missing={r['missing']}"
        line += f"   ${r['cost_cad']:.3f}"
        print(line)
    print("-" * 68)
    print(f"Products written : {store.count('product')}  (dedupe by ProductKey, ON CONFLICT DO UPDATE)")
    print(f"Listings written : {len(listings)}")
    print(f"Rejections       : {sum(1 for r in results if not r['status'].startswith('Accepted'))}"
          "   (counted separately, per PRD rule)")
    print(f"Cost by stage    : {ledger.by_stage()}")
    print(f"Total variable   : ${ledger.total():.3f}  "
          f"(avg ${ledger.total()/max(1,len(listings)):.3f}/completed listing; target ~$0.25)")
    print(f"Outputs          : {OUT.relative_to(Path(__file__).resolve().parent)}/product.csv, listing.csv, cost_log.jsonl")

    # redelivery check: reprocess submission 1's id -> must be a safe no-op
    replay = run(SUBMISSIONS[0], store, ledger, skus)
    same = replay == results[0]
    print(f"\nRedelivery check : resubmitting {SUBMISSIONS[0]['submission_id']} "
          f"-> identical result, no duplicate row: {same}")

    # show the rejection message + one enriched record snippet
    rej = next(r for r in results if not r["status"].startswith("Accepted"))
    print("\nRejection message (submission 3):")
    print("  " + rej["reason"])
    prod = results[0]["product"]
    print("\nSample enriched Product fields (flattened):")
    flat = flatten(prod)
    for f in ("ProductKey", "Brand", "ModelNumber", "InternalCategory",
              "OversizedFlag", "MasterTitle", "PricingAnchorPriceCAD", "HeroImageURL"):
        print(f"  {f:<22} = {flat[f]}")

    store.close()


if __name__ == "__main__":
    main()
