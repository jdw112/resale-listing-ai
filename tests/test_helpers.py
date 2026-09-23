"""Tests for resale_listing_ai/helpers.py's oversized_flag admin-override threading."""

from resale_listing_ai.helpers import oversized_flag


def test_oversized_flag_uses_hardcoded_default_when_no_override():
    assert oversized_flag(10, 10, 10, 1) == "No"
    assert oversized_flag(300, 10, 10, 1) == "Yes"  # exceeds default longest_side_cm


def test_oversized_flag_uses_admin_override_thresholds():
    config = {"business.oversized_ups_ca": {
        "longest_side_cm": 20, "length_plus_girth_cm": 1000, "weight_kg": 1000}}

    assert oversized_flag(25, 5, 5, 1, config=config) == "Yes"  # 25 > overridden 20
    assert oversized_flag(15, 5, 5, 1, config=config) == "No"


def test_oversized_flag_missing_dims_is_unable_to_determine_regardless_of_config():
    assert oversized_flag(None, 10, 10, 1, config={
        "business.oversized_ups_ca": {"longest_side_cm": 1, "length_plus_girth_cm": 1, "weight_kg": 1}
    }) == "Unable to Determine"
