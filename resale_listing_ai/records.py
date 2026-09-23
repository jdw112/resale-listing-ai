"""Schema-driven Product and Listing records, loaded from config/*.json.

The two records are the baselines (Appendix A/B) plus any
documented additions (see each schema's own field list and rules) — we
never hardcode the field list here, so the code and the Data Contract can
never drift apart."""

import csv
import json
from pathlib import Path

from .envelope import NULL

CONFIG = Path(__file__).resolve().parents[1] / "config"


def load_schema(name):
    return json.loads((CONFIG / name).read_text())


PRODUCT_SCHEMA = load_schema("product_schema.json")
LISTING_SCHEMA = load_schema("listing_schema.json")
PRODUCT_FIELDS = [f["name"] for f in PRODUCT_SCHEMA["fields"]]
LISTING_FIELDS = [f["name"] for f in LISTING_SCHEMA["fields"]]


def blank(fields):
    """A record with every schema field present and explicitly null."""
    return {name: NULL() for name in fields}


def blank_product():
    return blank(PRODUCT_FIELDS)


def blank_listing():
    return blank(LISTING_FIELDS)


def flatten(record):
    """Envelope -> plain value, for CSV export (confidence/source travel in JSON)."""
    return {k: (v["value"] if isinstance(v, dict) else v) for k, v in record.items()}


def write_csv(path, fields, records):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow(flatten(r))
