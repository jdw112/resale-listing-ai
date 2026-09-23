"""Deterministic helpers that need no external service: OversizedFlag and SKU."""

import json
import random
import string
from pathlib import Path

from . import admin_config

CONFIG = Path(__file__).resolve().parents[1] / "config"
_OVER = json.loads((CONFIG / "oversized_ups_ca.json").read_text())


def oversized_flag(pl_cm, pw_cm, ph_cm, weight_kg, config=None):
    """Derive OversizedFlag from packaged size/weight vs the UPS Canada baseline
    (admin-overridable via business.oversized_ups_ca). Package data missing ->
    'Unable to Determine' (never guessed), regardless of any override."""
    if None in (pl_cm, pw_cm, ph_cm, weight_kg):
        return "Unable to Determine"
    r = admin_config.get(config, "business.oversized_ups_ca", _OVER["rules"]["oversized_if_any"])
    dims = sorted([pl_cm, pw_cm, ph_cm])
    longest = dims[2]
    girth = 2 * (dims[0] + dims[1])           # 2 x (width + height)
    length_plus_girth = longest + girth
    if (longest >= r["longest_side_cm"]
            or length_plus_girth >= r["length_plus_girth_cm"]
            or weight_kg >= r["weight_kg"]):
        return "Yes"
    return "No"


def make_sku(existing=()):
    """Randomized, non-sequential, FNSKU-like identifier, unique within `existing`."""
    def block(n):
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))
    for _ in range(1000):
        sku = f"RL-{block(4)}-{block(3)}"
        if sku not in existing:
            return sku
    raise RuntimeError("could not allocate a unique SKU")
