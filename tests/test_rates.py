"""Tests for resale_listing_ai/rates.py — token-based cost from the config/model_rates.json
rate table (USD per 1M tokens) converted to CAD. Pure, reads the committed JSON."""

import pytest

from resale_listing_ai import rates


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.delenv("RESALE_LISTING_AI_USD_TO_CAD", raising=False)
    rates._load.cache_clear()
    yield
    rates._load.cache_clear()


def test_known_model_prices_input_and_output_and_converts_to_cad():
    # gpt-4o-mini: 0.15 in / 0.60 out USD per 1M; usd_to_cad 1.38 (config default).
    # (1000 * 0.15/1e6 + 500 * 0.60/1e6) * 1.38 = 0.00045 * 1.38 = 0.000621
    cost = rates.token_cost_cad("gpt-4o-mini", 1000, 500)
    assert cost == pytest.approx(0.000621, abs=1e-9)


def test_unknown_model_returns_none_so_caller_falls_back_to_flat_cost():
    assert rates.token_cost_cad("no-such-model", 1000, 500) is None


def test_usd_to_cad_env_override_applies_live():
    # override FX to 2.0 -> 0.00045 * 2.0 = 0.0009
    import os
    os.environ["RESALE_LISTING_AI_USD_TO_CAD"] = "2.0"
    cost = rates.token_cost_cad("gpt-4o-mini", 1000, 500)
    assert cost == pytest.approx(0.0009, abs=1e-9)


def test_missing_token_counts_treated_as_zero():
    assert rates.token_cost_cad("gpt-4o-mini", None, None) == 0.0
