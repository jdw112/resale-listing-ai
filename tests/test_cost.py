"""Tests for resale_listing_ai/cost.py CostLedger — the token/model detail added for
token-based cost tracking, alongside the existing flat cost_cad behavior."""

from resale_listing_ai.cost import CostLedger


def test_add_without_token_detail_leaves_fields_none_backward_compatible():
    ledger = CostLedger()
    ledger.add("s1", "identify", "vision", 0.030)
    row = ledger.rows[0]
    assert row["cost_cad"] == 0.030
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["model"] is None


def test_add_records_token_counts_and_model():
    ledger = CostLedger()
    ledger.add("s1", "identify", "openai_vision", 0.000621,
               input_tokens=1000, output_tokens=500, model="gpt-4o-mini")
    row = ledger.rows[0]
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["model"] == "gpt-4o-mini"


def test_tokens_by_stage_sums_input_and_output_per_stage():
    ledger = CostLedger()
    ledger.add("s1", "identify", "openai_vision", 0.01, input_tokens=1000, output_tokens=200, model="m")
    ledger.add("s1", "identify", "openai_vision", 0.01, input_tokens=500, output_tokens=100, model="m")
    ledger.add("s1", "copy", "openai_copy", 0.01, input_tokens=300, output_tokens=400, model="m")
    ledger.add("s1", "identify", "local_ocr", 0.0)  # no tokens -> contributes nothing

    by_stage = ledger.tokens_by_stage()
    assert by_stage["identify"] == {"input_tokens": 1500, "output_tokens": 300}
    assert by_stage["copy"] == {"input_tokens": 300, "output_tokens": 400}
