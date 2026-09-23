"""Tests for resale_listing_ai/admin_config.py — the key registry, validators, and the
two pure helpers (load_overrides, get) every overridable seam calls through."""

import json

import pytest

from resale_listing_ai import admin_config
from resale_listing_ai.db import Store


@pytest.fixture(autouse=True)
def _sqlite(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


def test_keys_covers_all_eleven_documented_keys():
    expected = {
        "provider_stack.vision_provider", "provider_stack.copy_provider",
        "provider_stack.web_search_provider", "provider_stack.image_process_provider",
        "provider_stack.json_repair_provider", "gate.threshold", "gate.mandatory_fields",
        "gate.core_fields", "business.categories", "business.condition_scale",
        "business.oversized_ups_ca",
    }
    assert set(admin_config.KEYS) == expected


def test_validate_value_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown config key"):
        admin_config.validate_value("not.a.real.key", "x")


@pytest.mark.parametrize("key,options_key", [
    ("provider_stack.vision_provider", "vision_options"),
    ("provider_stack.copy_provider", "copy_options"),
    ("provider_stack.image_process_provider", "image_process_options"),
    ("provider_stack.json_repair_provider", "json_repair_options"),
])
def test_validate_provider_keys_accept_their_documented_options(key, options_key):
    options = json.loads((admin_config.CONFIG_DIR / "provider_stack.json").read_text())[options_key]
    for value in options:
        admin_config.validate_value(key, value)  # must not raise


def test_validate_web_search_provider_accepts_its_options():
    options = json.loads((admin_config.CONFIG_DIR / "provider_stack.json").read_text())["options"]
    for value in options:
        admin_config.validate_value("provider_stack.web_search_provider", value)


def test_validate_provider_key_rejects_unlisted_value():
    with pytest.raises(ValueError):
        admin_config.validate_value("provider_stack.vision_provider", "not-a-real-provider")


def test_validate_gate_threshold_accepts_in_range_number():
    admin_config.validate_value("gate.threshold", 0.75)  # must not raise


@pytest.mark.parametrize("bad", [-0.1, 1.1, "0.7", True, None])
def test_validate_gate_threshold_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        admin_config.validate_value("gate.threshold", bad)


def test_validate_gate_mandatory_fields_accepts_known_field_names():
    admin_config.validate_value("gate.mandatory_fields", ["Brand", "ProductName"])


def test_validate_gate_mandatory_fields_rejects_unknown_field_name():
    with pytest.raises(ValueError, match="unknown field"):
        admin_config.validate_value("gate.mandatory_fields", ["Brand", "NotARealField"])


def test_validate_gate_mandatory_fields_rejects_listing_only_field_name():
    # SKU is a Listing-only field -- pipeline.gate() checks mandatory_fields
    # against `identity`, a Product-shaped dict, which can never contain it.
    # Accepting it here would permanently fail the gate for every submission.
    with pytest.raises(ValueError, match="unknown field"):
        admin_config.validate_value("gate.mandatory_fields", ["SKU"])


def test_validate_gate_core_fields_accepts_listing_only_field_name():
    # core_fields legitimately spans both Product and Listing fields --
    # AISuggestedConditionGrade lives on Listing and is already part of
    # pipeline.CORE_FIELDS's real default list.
    admin_config.validate_value("gate.core_fields", ["AISuggestedConditionGrade"])


def test_validate_gate_mandatory_fields_rejects_empty_list():
    with pytest.raises(ValueError):
        admin_config.validate_value("gate.mandatory_fields", [])


def test_validate_gate_core_fields_rejects_non_list():
    with pytest.raises(ValueError):
        admin_config.validate_value("gate.core_fields", "Brand")


def test_validate_business_categories_accepts_wellformed_map():
    admin_config.validate_value("business.categories", {"Smart Home": ["Cameras", "Sensors"]})


def test_validate_business_categories_rejects_non_string_subcategory():
    with pytest.raises(ValueError):
        admin_config.validate_value("business.categories", {"Smart Home": ["Cameras", 5]})


def test_validate_business_condition_scale_accepts_wellformed_value():
    admin_config.validate_value("business.condition_scale", {
        "provided_grades": ["New", "Used - Good"],
        "ai_suggested_grades": ["New", "Used - Good"],
    })


def test_validate_business_condition_scale_rejects_missing_key():
    with pytest.raises(ValueError):
        admin_config.validate_value("business.condition_scale", {"provided_grades": ["New"]})


def test_validate_business_oversized_ups_ca_accepts_wellformed_value():
    admin_config.validate_value("business.oversized_ups_ca", {
        "longest_side_cm": 274.32, "length_plus_girth_cm": 330, "weight_kg": 68,
    })


def test_validate_business_oversized_ups_ca_rejects_negative_number():
    with pytest.raises(ValueError):
        admin_config.validate_value("business.oversized_ups_ca", {
            "longest_side_cm": -1, "length_plus_girth_cm": 330, "weight_kg": 68,
        })


def test_load_overrides_returns_empty_dict_when_nothing_set(store):
    assert admin_config.load_overrides(store) == {}


def test_load_overrides_decodes_every_stored_value(store):
    store.set_admin_config_override("gate.threshold", json.dumps(0.75), updated_by=1)
    store.set_admin_config_override(
        "provider_stack.vision_provider", json.dumps("gemini"), updated_by=1)

    config = admin_config.load_overrides(store)

    assert config == {"gate.threshold": 0.75, "provider_stack.vision_provider": "gemini"}


def test_get_returns_default_when_config_is_none():
    assert admin_config.get(None, "gate.threshold", 0.6) == 0.6


def test_get_returns_default_when_key_not_overridden():
    assert admin_config.get({}, "gate.threshold", 0.6) == 0.6


def test_get_returns_override_when_present():
    assert admin_config.get({"gate.threshold": 0.8}, "gate.threshold", 0.6) == 0.8
