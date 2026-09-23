"""Token-based cost — prices an LLM call from its token usage against the
config/model_rates.json rate table (USD per 1,000,000 tokens) and converts to
the ledger's CAD.

token_cost_cad() returns None for a model absent from the table; callers read
that as "fall back to the provider's flat unit cost" (resale_listing_ai/adapters.py's
_call_with_cost), so an unpriced model never breaks a run."""

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_RATES_PATH = CONFIG_DIR / "model_rates.json"


@lru_cache(maxsize=1)
def _load():
    return json.loads(_RATES_PATH.read_text())


def _usd_to_cad():
    """FX factor: RESALE_LISTING_AI_USD_TO_CAD env override wins over the file value,
    resolved per call so an operator can retune it without restarting."""
    env = os.environ.get("RESALE_LISTING_AI_USD_TO_CAD")
    if env:
        return float(env)
    return float(_load().get("usd_to_cad", 1.0))


def token_cost_cad(model, input_tokens, output_tokens):
    """Cost in CAD for a call on `model` that consumed the given token counts,
    or None when `model` is not in the rate table (caller falls back to flat)."""
    rate = _load().get("models", {}).get(model)
    if rate is None:
        return None
    usd = ((input_tokens or 0) * rate["input_usd_per_1m"]
           + (output_tokens or 0) * rate["output_usd_per_1m"]) / 1_000_000
    # 6dp: per-call token costs are sub-cent; 4dp (the ledger's storage
    # rounding) would erase most of the value. Raw token counts are stored
    # alongside so cost stays exactly re-derivable regardless.
    return round(usd * _usd_to_cad(), 6)
