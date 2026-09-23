"""Tests for resale_listing_ai/findings.py — the batch runner over samples/ (run_findings.py).

Pure/offline throughout: no real API keys, no live Postgres. Discovery tests use a
tmp_path-built fake samples/ tree rather than the real one, so they pass on a
fresh clone before samples/_build_samples.py has ever been run and regardless of
whether the (git-ignored) real "Product NN" folders are present.
"""

import json


# ---------------------------------------------------------------------------
# mock_evidence.json -> adapters._KNOWN[hint] shape
# ---------------------------------------------------------------------------

def test_mock_evidence_to_known_passes_product_fields_through_as_identity():
    from resale_listing_ai.findings import mock_evidence_to_known

    evidence = {"barcodes": [], "product_fields": {"Brand": {"value": "Acme", "confidence": 0.9, "source": "vision"}},
                "listing_fields": {}, "serial_readable": False, "pricing_comps": []}

    known = mock_evidence_to_known(evidence)

    assert known["identity"] == {"Brand": {"value": "Acme", "confidence": 0.9, "source": "vision"}}


def test_mock_evidence_to_known_passes_listing_fields_through_as_unit():
    from resale_listing_ai.findings import mock_evidence_to_known

    evidence = {"barcodes": [], "product_fields": {},
                "listing_fields": {"AISuggestedConditionGrade": {"value": "Open Box", "confidence": 0.8, "source": "vision"}},
                "serial_readable": False, "pricing_comps": []}

    known = mock_evidence_to_known(evidence)

    assert known["unit"] == {"AISuggestedConditionGrade": {"value": "Open Box", "confidence": 0.8, "source": "vision"}}


def test_mock_evidence_to_known_no_barcodes_gives_none():
    from resale_listing_ai.findings import mock_evidence_to_known

    known = mock_evidence_to_known({"barcodes": [], "product_fields": {}, "listing_fields": {},
                                     "serial_readable": False, "pricing_comps": []})

    assert known["barcode"] is None


def test_mock_evidence_to_known_first_barcode_used():
    from resale_listing_ai.findings import mock_evidence_to_known

    known = mock_evidence_to_known({"barcodes": ["012345678905"], "product_fields": {}, "listing_fields": {},
                                     "serial_readable": False, "pricing_comps": []})

    assert known["barcode"] == "012345678905"


# ---------------------------------------------------------------------------
# pricing_comps -> PricingAnchor*/CorroboratingSource*/PricingConfidence
# ---------------------------------------------------------------------------

def test_convert_pricing_comps_empty_returns_no_pricing_fields():
    from resale_listing_ai.findings import _convert_pricing_comps

    assert _convert_pricing_comps([]) == {}


def test_convert_pricing_comps_single_comp_sets_anchor_with_medium_confidence():
    from resale_listing_ai.findings import _convert_pricing_comps

    out = _convert_pricing_comps([
        {"price_cad": 149.99, "source_name": "Best Buy Canada", "source_url": "https://bestbuy.ca/x", "exact_match": True},
    ])

    assert out["PricingAnchorPriceCAD"]["value"] == 149.99
    assert out["PricingAnchorSourceName"]["value"] == "Best Buy Canada"
    assert out["PricingConfidence"]["value"] == "Medium"
    assert "CorroboratingSourcePriceCAD" not in out


def test_convert_pricing_comps_two_agreeing_comps_sets_high_confidence():
    from resale_listing_ai.findings import _convert_pricing_comps

    out = _convert_pricing_comps([
        {"price_cad": 249.99, "source_name": "A", "source_url": "https://a", "exact_match": True},
        {"price_cad": 239.99, "source_name": "B", "source_url": "https://b", "exact_match": True},
    ])

    assert out["PricingConfidence"]["value"] == "High"
    assert out["CorroboratingSourcePriceCAD"]["value"] == 239.99


def test_convert_pricing_comps_two_disagreeing_comps_sets_low_confidence():
    """Sources disagreeing by a wide margin must surface as a conflict (lower
    confidence) — never silently averaged, never one silently trusted."""
    from resale_listing_ai.findings import _convert_pricing_comps

    out = _convert_pricing_comps([
        {"price_cad": 249.99, "source_name": "Acme Store", "source_url": "https://a", "exact_match": True},
        {"price_cad": 89.99, "source_name": "Reseller X", "source_url": "https://b", "exact_match": True},
    ])

    assert out["PricingConfidence"]["value"] == "Low"
    assert out["PricingAnchorPriceCAD"]["value"] == 249.99
    assert out["CorroboratingSourcePriceCAD"]["value"] == 89.99


# ---------------------------------------------------------------------------
# Sample discovery — offline, tmp_path-built fake tree
# ---------------------------------------------------------------------------

def _write_mock_sample(root, name):
    d = root / name
    d.mkdir(parents=True)
    (d / "submission.json").write_text(json.dumps({
        "submission_id": name, "notes": None, "provided_condition_grade": "Open Box", "images": ["img_1.jpg"]}))
    (d / "mock_evidence.json").write_text(json.dumps(
        {"barcodes": [], "product_fields": {}, "listing_fields": {}, "serial_readable": False, "pricing_comps": []}))
    (d / "img_1.jpg").write_bytes(b"fake")


def test_discover_mock_submissions_finds_folders_with_both_json_files(tmp_path):
    from resale_listing_ai.findings import discover_mock_submissions

    _write_mock_sample(tmp_path, "000412_nest_doorbell")
    (tmp_path / "not_a_sample").mkdir()  # no submission.json/mock_evidence.json -> ignored

    found = discover_mock_submissions(tmp_path)

    assert [d.name for d in found] == ["000412_nest_doorbell"]


def test_discover_mock_submissions_empty_dir_returns_empty_list(tmp_path):
    from resale_listing_ai.findings import discover_mock_submissions

    assert discover_mock_submissions(tmp_path) == []


def test_discover_mock_submissions_missing_samples_dir_returns_empty_list_not_an_error(tmp_path):
    from resale_listing_ai.findings import discover_mock_submissions

    assert discover_mock_submissions(tmp_path / "does-not-exist") == []


def test_discover_real_product_folders_finds_product_dirs_with_jpgs(tmp_path):
    from resale_listing_ai.findings import discover_real_product_folders

    d = tmp_path / "Product 01"
    d.mkdir()
    (d / "photo.jpg").write_bytes(b"fake")
    (d / "Notes.txt").write_text("PRODUCT: Widget")
    (tmp_path / "Product 02 (empty, not downloaded)").mkdir()  # no jpgs -> not counted

    found = discover_real_product_folders(tmp_path)

    assert [d.name for d in found] == ["Product 01"]


def test_discover_real_product_folders_absent_returns_empty_list_not_an_error(tmp_path):
    """Real client photos are git-ignored — a fresh clone must not error, just
    report zero real folders available."""
    from resale_listing_ai.findings import discover_real_product_folders

    assert discover_real_product_folders(tmp_path / "does-not-exist") == []


def test_load_mock_submission_resolves_image_paths_and_sets_hint(tmp_path):
    from resale_listing_ai.findings import load_mock_submission

    _write_mock_sample(tmp_path, "000412_nest_doorbell")

    submission, hint, evidence = load_mock_submission(tmp_path / "000412_nest_doorbell")

    assert submission["submission_id"] == "000412_nest_doorbell"
    assert submission["hint"] == "000412_nest_doorbell"
    assert submission["images"] == [str(tmp_path / "000412_nest_doorbell" / "img_1.jpg")]
    assert evidence == {"barcodes": [], "product_fields": {}, "listing_fields": {},
                         "serial_readable": False, "pricing_comps": []}
