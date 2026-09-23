"""Tests for the field-enrichment changes: spec-sheet dimension lookup
(measures_lookup / _map_measures_output) and the Listing operational defaults."""

from resale_listing_ai import adapters


def test_literal_null_string_is_normalized_to_null():
    # Models sometimes emit the string "null" instead of a JSON null; it must not
    # be stored as text. "None" stays a real answer, though.
    assert adapters._is_blank_value("null")
    assert adapters._is_blank_value("  NULL ")
    assert adapters._is_blank_value("n/a")
    assert adapters._is_blank_value(None)
    assert not adapters._is_blank_value("None")
    assert not adapters._is_blank_value("AcmeFlow")

    v = adapters._map_vision_output({"Brand": {"value": "null", "confidence": 0.8, "source": "vision"}})
    assert v["Brand"] == {"value": None, "confidence": 0.0, "source": "none"}
    u = adapters._map_unit_output({"TestingStatus": {"value": "null", "confidence": 0.5, "source": "context"}})
    assert u["TestingStatus"]["value"] is None


def test_map_measures_output_fills_dimension_fields():
    raw = {
        "match_found": True,
        "source_url": "https://maker.example/spec.pdf",
        "unpackaged": {"length_cm": 10.0, "width_cm": 5.0, "height_cm": 2.5, "weight_kg": 0.3},
        "packaged": {"length_cm": 12.0, "width_cm": 6.0, "height_cm": 3.0, "weight_kg": 0.4},
    }
    out = adapters._map_measures_output(raw)
    assert out["PackagedLengthCM"]["value"] == 12.0
    assert out["PackagedLengthCM"]["source"] == "external_lookup"
    assert out["UnpackagedWeightKG"]["value"] == 0.3
    assert out["ProductSpecificationsSourceURL"]["value"] == "https://maker.example/spec.pdf"


def test_map_measures_output_no_match_is_empty():
    assert adapters._map_measures_output({"match_found": False, "source_url": None,
                                          "unpackaged": None, "packaged": None}) == {}
    assert adapters._map_measures_output(None) == {}


def test_map_measures_output_skips_null_figures():
    raw = {
        "match_found": True, "source_url": None,
        "unpackaged": {"length_cm": 10.0, "width_cm": None, "height_cm": None, "weight_kg": None},
        "packaged": None,
    }
    out = adapters._map_measures_output(raw)
    assert out["UnpackagedLengthCM"]["value"] == 10.0
    assert "UnpackagedWidthCM" not in out
    # source_url only attached when a dimension was actually found AND a url given
    assert "ProductSpecificationsSourceURL" not in out


def test_measures_lookup_stub_mode_unchanged(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    # Unknown hint -> empty in stub mode, no network.
    assert adapters.measures_lookup({"images": [], "hint": "nope"}) == {}


def test_listing_defaults_fill_blanks_in_stub_pipeline(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")  # offline; never the real Postgres
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from resale_listing_ai.db import Store
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import pipeline

    store = Store.connect()
    store.ensure_schema()
    sub = {
        "submission_id": "enrich-1", "images": ["a.jpg"], "notes": None,
        "provided_condition_grade": "New", "hint": "nest-doorbell",
    }
    terminal, payload = pipeline._stage_ingest(sub, store, CostLedger(), set())
    assert not terminal, payload
    listing = store.get_listing(payload["sku"])
    assert listing["TestingStatus"]["value"] == "Not Tested"
    assert listing["TestingStatus"]["source"] == "derived"
    assert listing["MissingItems"]["value"] == "None noted"
    store.close()
