"""Tests for the real (non-stub) adapter implementations in resale_listing_ai/adapters.py.

Fixtures under tests/fixtures/:
  barcode_ean13.png    real EAN13 barcode "012345678905" (python-barcode + Pillow),
                       decodes via pyzbar to "0123456789050" (13-digit incl. check digit)
  blank_no_barcode.jpg plain white square, no barcode present

These tests exercise the real code paths (RESALE_LISTING_AI_STUBS unset), not the offline stub
fallback, which the pipeline tests in test_pipeline.py cover separately.
"""

from pathlib import Path

import pytest

from resale_listing_ai.envelope import SOURCES
from resale_listing_ai.records import PRODUCT_FIELDS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BARCODE_IMAGE = str(FIXTURES / "barcode_ean13.png")
BLANK_IMAGE = str(FIXTURES / "blank_no_barcode.jpg")


# ---------------------------------------------------------------------------
# Schema shape tests
# ---------------------------------------------------------------------------

def test_hero_image_source_url_field_exists_in_product_schema():
    from resale_listing_ai.records import PRODUCT_FIELDS, blank_product

    assert "HeroImageSourceURL" in PRODUCT_FIELDS
    blank = blank_product()
    assert blank["HeroImageSourceURL"] == {"value": None, "confidence": 0.0, "source": "none"}


def test_website_hero_image_url_field_exists_in_product_schema():
    from resale_listing_ai.records import PRODUCT_FIELDS, blank_product

    assert "WebsiteHeroImageURL" in PRODUCT_FIELDS
    assert "WebsiteHeroImageURL" in blank_product()


def test_website_hero_review_required_field_exists_in_product_schema():
    from resale_listing_ai.records import PRODUCT_FIELDS, blank_product

    assert "WebsiteHeroReviewRequired" in PRODUCT_FIELDS
    assert "WebsiteHeroReviewRequired" in blank_product()


# ---------------------------------------------------------------------------
# _load_prompt / _validate_strict_schema — config/prompts/ infrastructure
# ---------------------------------------------------------------------------

def test_load_prompt_reads_json_from_the_prompts_dir(tmp_path, monkeypatch):
    from resale_listing_ai import adapters

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "example.json").write_text('{"instructions": "hello"}')
    monkeypatch.setattr(adapters, "PROMPTS_DIR", prompts_dir)

    assert adapters._load_prompt("example") == {"instructions": "hello"}


def test_editing_a_prompt_file_changes_what_the_next_load_returns(tmp_path, monkeypatch):
    """Proves config/prompts/*.json is the live source of truth for a prompt,
    not a value baked in once and forgotten -- the actual claim behind
    'editable without a rebuild'. Representative of all 7 prompts, since they
    all go through this same _load_prompt() mechanism."""
    import json
    import shutil

    from resale_listing_ai import adapters

    real_file = adapters.PROMPTS_DIR / "vision_identify.json"
    tmp_prompts_dir = tmp_path / "prompts"
    tmp_prompts_dir.mkdir()
    shutil.copy(real_file, tmp_prompts_dir / "vision_identify.json")
    monkeypatch.setattr(adapters, "PROMPTS_DIR", tmp_prompts_dir)

    original = adapters._load_prompt("vision_identify")
    assert original["instructions"] == adapters.VISION_INSTRUCTIONS  # sanity check

    (tmp_prompts_dir / "vision_identify.json").write_text(
        json.dumps({**original, "instructions": "EDITED INSTRUCTIONS FOR THIS TEST"}))

    reloaded = adapters._load_prompt("vision_identify")
    assert reloaded["instructions"] == "EDITED INSTRUCTIONS FOR THIS TEST"
    assert reloaded["fields"] == original["fields"]  # everything else unchanged


def test_load_prompt_raises_a_clear_error_on_malformed_json(tmp_path, monkeypatch):
    from resale_listing_ai import adapters

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "broken.json").write_text('{"instructions": "hello",}')  # trailing comma
    monkeypatch.setattr(adapters, "PROMPTS_DIR", prompts_dir)

    with pytest.raises(ValueError, match="broken.json"):
        adapters._load_prompt("broken")


def test_load_prompt_raises_a_clear_error_on_a_missing_instructions_key(tmp_path, monkeypatch):
    from resale_listing_ai import adapters

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "incomplete.json").write_text('{"fields": []}')
    monkeypatch.setattr(adapters, "PROMPTS_DIR", prompts_dir)

    with pytest.raises(ValueError, match="instructions"):
        adapters._load_prompt("incomplete")


def test_validate_strict_schema_accepts_a_valid_schema():
    from resale_listing_ai import adapters

    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    adapters._validate_strict_schema(schema, "example")  # must not raise


def test_validate_strict_schema_rejects_missing_additional_properties_false():
    from resale_listing_ai import adapters

    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    with pytest.raises(ValueError, match="additionalProperties"):
        adapters._validate_strict_schema(schema, "example")


def test_validate_strict_schema_rejects_required_not_matching_properties():
    from resale_listing_ai import adapters

    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}, "extra": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match="required"):
        adapters._validate_strict_schema(schema, "example")


def test_validate_strict_schema_recurses_into_nested_objects():
    from resale_listing_ai import adapters

    schema = {
        "type": "object",
        "properties": {
            "anchor": {
                "type": "object",
                "properties": {"price": {"type": "number"}},
                "required": [],  # missing "price" -> must fail on the NESTED node
                "additionalProperties": False,
            },
        },
        "required": ["anchor"],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match="anchor"):
        adapters._validate_strict_schema(schema, "example")


def test_validate_strict_schema_recurses_into_nullable_object_nodes():
    """Several real schemas (RETAIL_PRICE_SCHEMA's anchor/corroborating,
    RESALE_PRICE_SCHEMA's comparable, image_classify's clutter_bbox) write
    nullable objects as {"type": ["object", "null"]}, not the bare string
    "object" -- the validator must still recurse into these, not skip them."""
    from resale_listing_ai import adapters

    schema = {
        "type": "object",
        "properties": {
            "anchor": {
                "type": ["object", "null"],
                "properties": {"price": {"type": "number"}},
                "required": [],  # missing "price" -> must still fail
                "additionalProperties": False,
            },
        },
        "required": ["anchor"],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match="anchor"):
        adapters._validate_strict_schema(schema, "example")


def test_validate_strict_schema_recurses_into_object_nodes_with_implicit_type():
    from resale_listing_ai import adapters

    schema = {
        "properties": {
            "anchor": {
                "properties": {"price": {"type": "number"}},
                "required": [],  # missing "price" -> must still fail, even with no "type" key
                "additionalProperties": False,
            },
        },
        "required": ["anchor"],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match="anchor"):
        adapters._validate_strict_schema(schema, "example")


# ---------------------------------------------------------------------------
# barcode_read — real pyzbar decoding
# ---------------------------------------------------------------------------

def test_barcode_read_decodes_known_fixture_image():
    from resale_listing_ai import adapters

    env = adapters.barcode_read({"submission_id": "x", "images": [BARCODE_IMAGE]})

    assert env["value"] == "0123456789050"
    assert env["source"] == "barcode"
    assert 0.0 < env["confidence"] <= 1.0


def test_barcode_read_returns_null_when_no_barcode_present():
    from resale_listing_ai import adapters

    env = adapters.barcode_read({"submission_id": "x", "images": [BLANK_IMAGE]})

    assert env["value"] is None
    assert env["confidence"] == 0.0
    assert env["source"] == "none"


def test_barcode_read_returns_null_never_autocorrects_on_bad_path():
    from resale_listing_ai import adapters

    # a missing/unreadable image must never be guessed at -> NULL, not an exception
    env = adapters.barcode_read({"submission_id": "x", "images": ["/no/such/file.jpg"]})

    assert env["value"] is None
    assert env["source"] == "none"


# ---------------------------------------------------------------------------
# ocr_read — real pytesseract label-text extraction
# ---------------------------------------------------------------------------

def test_ocr_read_returns_empty_string_in_stub_mode(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")

    assert adapters.ocr_read({"submission_id": "x", "images": [BARCODE_IMAGE]}) == ""


def test_ocr_read_extracts_text_via_pytesseract(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setattr(
        "pytesseract.image_to_string",
        lambda img: "Model No. AC1000-XY\nMade in China",
    )

    text = adapters.ocr_read({"submission_id": "x", "images": [BARCODE_IMAGE]})

    assert text == "Model No. AC1000-XY\nMade in China"


def test_ocr_read_skips_unreadable_image_never_raises(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)

    # a missing/unreadable image must never be guessed at or raised on -> ""
    text = adapters.ocr_read({"submission_id": "x", "images": ["/no/such/file.jpg"]})

    assert text == ""


def test_ocr_read_never_raises_when_tesseract_binary_missing(monkeypatch):
    import pytesseract

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)

    def _raise(img):
        raise pytesseract.TesseractNotFoundError("tesseract is not installed")

    monkeypatch.setattr("pytesseract.image_to_string", _raise)

    # a valid, readable image but a missing system tesseract binary must
    # still never raise out of ocr_read -- this is an optional accuracy hint,
    # not a required step, and must never take down the real-mode ingest path.
    text = adapters.ocr_read({"submission_id": "x", "images": [BARCODE_IMAGE]})

    assert text == ""


def test_ocr_read_returns_empty_string_when_no_images():
    from resale_listing_ai import adapters

    assert adapters.ocr_read({"submission_id": "x", "images": []}) == ""


def test_ocr_read_joins_multiple_images_with_blank_line_separation(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    calls = iter(["front label text", "back label text"])
    monkeypatch.setattr("pytesseract.image_to_string", lambda img: next(calls))

    text = adapters.ocr_read({"submission_id": "x", "images": [BARCODE_IMAGE, BLANK_IMAGE]})

    assert text == "front label text\n\nback label text"


def test_ocr_read_skips_images_with_no_text_found(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    calls = iter(["", "back label text"])  # first image: blank/no text
    monkeypatch.setattr("pytesseract.image_to_string", lambda img: next(calls))

    text = adapters.ocr_read({"submission_id": "x", "images": [BLANK_IMAGE, BARCODE_IMAGE]})

    assert text == "back label text"


def test_ocr_read_truncates_each_image_to_the_char_cap(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setattr("pytesseract.image_to_string", lambda img: "x" * 5000)

    text = adapters.ocr_read({"submission_id": "x", "images": [BARCODE_IMAGE]})

    assert len(text) == adapters._OCR_MAX_CHARS_PER_IMAGE
    assert text == "x" * adapters._OCR_MAX_CHARS_PER_IMAGE


# ---------------------------------------------------------------------------
# vision_identify — output mapping onto real Product schema field names
# ---------------------------------------------------------------------------

RAW_VISION_OUTPUT = {
    "Brand": {"value": "Acme", "confidence": 0.92, "source": "vision"},
    "Manufacturer": {"value": "Acme Corp", "confidence": 0.85, "source": "vision"},
    "ProductName": {"value": "Smart Doorbell (wired, 2nd gen)", "confidence": 0.9, "source": "vision"},
    "ProductType": {"value": "Wired video doorbell", "confidence": 0.8, "source": "vision"},
    "ModelNumber": {"value": "AC1000-XY", "confidence": 0.94, "source": "label_ocr"},
    "MPN": {"value": "AC1000-XY", "confidence": 0.94, "source": "label_ocr"},
    "Variant": {"value": "Wired, 2nd generation", "confidence": 0.75, "source": "vision"},
    "Colour": {"value": "Ash", "confidence": 0.8, "source": "vision"},
    "Material": {"value": "Plastic", "confidence": 0.7, "source": "vision"},
    "InternalCategory": {"value": "Smart Home", "confidence": 0.88, "source": "vision"},
    "InternalSubcategory": {"value": "Video Doorbells", "confidence": 0.86, "source": "vision"},
}


def test_vision_identify_prompt_config_matches_the_loaded_constants():
    from resale_listing_ai import adapters

    prompt = adapters._load_prompt("vision_identify")

    assert adapters.IDENTITY_FIELDS == [
        "Brand", "Manufacturer", "ProductName", "ProductType", "ModelNumber", "MPN",
        "Variant", "Colour", "Material", "Finish", "ProductLineOrSeries",
        "InternalCategory", "InternalSubcategory",
        "StandardIncludedItems", "CompatibilityNotes", "InstallationRequirements",
        "CategorySpecificSpecifications",
    ]
    assert "never guess" in adapters.VISION_INSTRUCTIONS
    assert adapters.VISION_JSON_SCHEMA["required"] == adapters.IDENTITY_FIELDS
    assert adapters.VISION_JSON_SCHEMA["properties"]["Brand"] == prompt["field_schema"]


def test_map_vision_output_maps_onto_real_schema_field_names():
    from resale_listing_ai import adapters

    mapped = adapters._map_vision_output(RAW_VISION_OUTPUT)

    assert set(mapped) <= set(PRODUCT_FIELDS)
    assert mapped["Brand"] == {"value": "Acme", "confidence": 0.92, "source": "vision"}
    assert mapped["ModelNumber"]["source"] == "label_ocr"


def test_map_vision_output_nulls_missing_or_blank_fields():
    from resale_listing_ai import adapters

    mapped = adapters._map_vision_output({"Brand": {"value": "", "confidence": 0.9, "source": "vision"}})

    assert mapped["Brand"] == {"value": None, "confidence": 0.0, "source": "none"}
    assert mapped["ProductType"] == {"value": None, "confidence": 0.0, "source": "none"}


class _FakeResponse:
    def __init__(self, output_text):
        self.output_text = output_text


class _FakeResponses:
    def __init__(self, output_text):
        self._output_text = output_text
        self.last_call = None

    def create(self, **kwargs):
        self.last_call = kwargs
        return _FakeResponse(self._output_text)


class _FakeOpenAIClient:
    def __init__(self, output_text):
        self.responses = _FakeResponses(output_text)


class _FakeAnthropicBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicMessage:
    def __init__(self, text):
        self.content = [_FakeAnthropicBlock(text)]


class _FakeAnthropicMessages:
    def __init__(self, text):
        self._text = text
        self.last_call = None

    def create(self, **kwargs):
        self.last_call = kwargs
        return _FakeAnthropicMessage(self._text)


class _FakeAnthropicClient:
    def __init__(self, text):
        self.messages = _FakeAnthropicMessages(text)


def test_vision_identify_real_mode_returns_valid_envelopes(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps(RAW_VISION_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())  # any real image file is fine here
    submission = {"submission_id": "x", "images": [str(img)], "notes": "40-foot model"}

    identity = adapters.vision_identify(submission)

    assert fake_client.responses.last_call is not None, "must actually call the Responses API"
    assert set(identity) <= set(PRODUCT_FIELDS)
    for field, env in identity.items():
        assert set(env) == {"value", "confidence", "source"}
        assert env["source"] in SOURCES
        assert 0.0 <= env["confidence"] <= 1.0
    assert identity["Brand"]["value"] == "Acme"


def test_vision_identify_real_mode_logs_token_based_cost(monkeypatch, tmp_path):
    """When the Responses API returns usage, the ledger row bills the real
    token cost (config/model_rates.json for VISION_MODEL) and records tokens."""
    import json
    from types import SimpleNamespace

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setattr(adapters, "VISION_MODEL", "gpt-4o-mini")

    class _RespWithUsage(_FakeResponses):
        def create(self, **kwargs):
            self.last_call = kwargs
            return SimpleNamespace(
                output_text=self._output_text,
                usage=SimpleNamespace(input_tokens=1000, output_tokens=500))

    fake_client = _FakeOpenAIClient(json.dumps(RAW_VISION_OUTPUT))
    fake_client.responses = _RespWithUsage(json.dumps(RAW_VISION_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()
    adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None}, ledger=ledger)

    row = next(r for r in ledger.rows if r["stage"] == "identify" and r["service"] == "openai_vision")
    assert row["model"] == "gpt-4o-mini"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cost_cad"] == 0.0006  # token cost, not the flat 0.030


def test_vision_identify_real_mode_recovers_from_malformed_output_via_repair(monkeypatch, tmp_path):
    """The model returning prose instead of JSON must not crash the stage — the
    JSON-repair step at this stage boundary recovers what it safely can and nulls
    the rest, rather than raising or fabricating."""
    import json

    from resale_listing_ai import adapters, json_repair

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_openai = _FakeOpenAIClient("Sure — this looks like a Acme Smart Doorbell, wired.")
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_openai)

    repaired = {f: {"value": None, "confidence": 0.0, "source": "vision"} for f in adapters.IDENTITY_FIELDS}
    repaired["Brand"] = {"value": "Acme", "confidence": 0.6, "source": "vision"}
    fake_anthropic = _FakeAnthropicClient(json.dumps(repaired))
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_anthropic)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    identity = adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})

    assert identity["Brand"]["value"] == "Acme"
    assert identity["ProductName"]["value"] is None  # not recovered -> stays null, never guessed


def test_vision_identify_openai_prompt_includes_ocr_text_when_present(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps(RAW_VISION_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    submission = {"submission_id": "x", "images": [str(img)], "notes": None}

    adapters.vision_identify(submission, ocr_text="Model No. AC1000-XY")

    content = fake_client.responses.last_call["input"][0]["content"]
    instructions_block = content[0]["text"]
    assert "OCR-extracted label text" in instructions_block
    assert "Model No. AC1000-XY" in instructions_block


def test_vision_identify_openai_prompt_unchanged_when_ocr_text_empty(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps(RAW_VISION_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    submission = {"submission_id": "x", "images": [str(img)], "notes": None}

    adapters.vision_identify(submission, ocr_text="")

    content = fake_client.responses.last_call["input"][0]["content"]
    assert content[0]["text"] == adapters.VISION_INSTRUCTIONS


def test_vision_identify_prompt_unchanged_when_ocr_text_not_passed(monkeypatch, tmp_path):
    """ocr_text defaults to None -> existing callers with no knowledge of it
    (any test or caller written before this task) see byte-identical behavior."""
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps(RAW_VISION_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    submission = {"submission_id": "x", "images": [str(img)], "notes": None}

    adapters.vision_identify(submission)

    content = fake_client.responses.last_call["input"][0]["content"]
    assert content[0]["text"] == adapters.VISION_INSTRUCTIONS


def test_instructions_with_ocr_appends_labeled_block():
    from resale_listing_ai import adapters

    result = adapters._instructions_with_ocr("BASE", "some label text")

    assert result.startswith("BASE")
    assert "OCR-extracted label text" in result
    assert "some label text" in result


def test_instructions_with_ocr_passthrough_when_empty():
    from resale_listing_ai import adapters

    assert adapters._instructions_with_ocr("BASE", "") == "BASE"
    assert adapters._instructions_with_ocr("BASE", None) == "BASE"


def test_vision_identify_requires_api_key_when_not_stubbed(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        adapters.vision_identify({"submission_id": "x", "images": [], "notes": None})


# ---------------------------------------------------------------------------
# unit_observe — the observed-PHYSICAL-UNIT counterpart to vision_identify
# (condition/completeness/defects/testing), mapped onto real Listing schema
# field names.
# ---------------------------------------------------------------------------

RAW_UNIT_OUTPUT = {
    "SerialNumber": {"value": "SN-AC1000-8841", "confidence": 0.9, "source": "label_ocr"},
    "AISuggestedConditionGrade": {"value": "Open Box - Like New", "confidence": 0.8, "source": "vision"},
    "ConditionDetails": {"value": "Minor scuff on back panel, otherwise clean", "confidence": 0.7, "source": "vision"},
    "VisibleWear": {"value": "Light scuffing on back panel", "confidence": 0.7, "source": "vision"},
    "ActualIncludedItems": {"value": "Doorbell, base plate, wedge, chime puck, hex key", "confidence": 0.8, "source": "vision"},
    "MissingItems": {"value": "None observed", "confidence": 0.6, "source": "vision"},
    "MissingComponents": {"value": "None observed", "confidence": 0.6, "source": "vision"},
    "OriginalPackagingIncluded": {"value": "Yes", "confidence": 0.85, "source": "vision"},
    "OriginalBoxIncluded": {"value": "Yes", "confidence": 0.85, "source": "vision"},
    "MissingHardware": {"value": "No", "confidence": 0.8, "source": "vision"},
    "DefectsOrDamage": {"value": "No visible cracks or lens damage", "confidence": 0.75, "source": "vision"},
    "FunctionalConcerns": {"value": "None reported", "confidence": 0.5, "source": "context"},
    "TestingStatus": {"value": "Tested by submitter, powers on", "confidence": 0.7, "source": "context"},
    "TestsPerformed": {"value": "Powered on and connected to app per notes", "confidence": 0.7, "source": "context"},
    "TestResults": {"value": "Functions normally per submitter", "confidence": 0.7, "source": "context"},
    "UnitSpecificNotes": {"value": "Submitter reports light use", "confidence": 0.6, "source": "context"},
}


def test_unit_observe_prompt_config_matches_the_loaded_constants():
    from resale_listing_ai import adapters

    prompt = adapters._load_prompt("unit_observe")

    assert adapters.UNIT_FIELDS == [
        "SerialNumber", "AISuggestedConditionGrade", "ConditionDetails", "VisibleWear",
        "ActualIncludedItems", "MissingItems", "MissingComponents",
        "OriginalPackagingIncluded", "OriginalBoxIncluded", "MissingHardware",
        "DefectsOrDamage", "FunctionalConcerns", "TestingStatus", "TestsPerformed",
        "TestResults", "UnitSpecificNotes",
    ]
    assert "context" in adapters.UNIT_INSTRUCTIONS
    assert "never infer functional status" in adapters.UNIT_INSTRUCTIONS
    assert adapters.UNIT_JSON_SCHEMA["required"] == adapters.UNIT_FIELDS
    assert adapters.UNIT_JSON_SCHEMA["properties"]["SerialNumber"] == prompt["field_schema"]


def test_map_unit_output_maps_onto_real_schema_field_names():
    from resale_listing_ai import adapters
    from resale_listing_ai.records import LISTING_FIELDS

    mapped = adapters._map_unit_output(RAW_UNIT_OUTPUT)

    assert set(mapped) <= set(LISTING_FIELDS)
    assert mapped["SerialNumber"] == {"value": "SN-AC1000-8841", "confidence": 0.9, "source": "label_ocr"}
    assert mapped["TestingStatus"]["source"] == "context"


def test_map_unit_output_nulls_missing_or_blank_fields():
    from resale_listing_ai import adapters

    mapped = adapters._map_unit_output(
        {"SerialNumber": {"value": "", "confidence": 0.9, "source": "label_ocr"}})

    assert mapped["SerialNumber"] == {"value": None, "confidence": 0.0, "source": "none"}
    assert mapped["AISuggestedConditionGrade"] == {"value": None, "confidence": 0.0, "source": "none"}


def test_unit_observe_real_mode_returns_valid_envelopes(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.records import LISTING_FIELDS

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps(RAW_UNIT_OUTPUT))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    submission = {"submission_id": "x", "images": [str(img)], "notes": "powers on fine"}

    unit = adapters.unit_observe(submission)

    assert fake_client.responses.last_call is not None, "must actually call the Responses API"
    assert set(unit) <= set(LISTING_FIELDS)
    for field, env in unit.items():
        assert set(env) == {"value", "confidence", "source"}
        assert env["source"] in SOURCES
        assert 0.0 <= env["confidence"] <= 1.0
    assert unit["SerialNumber"]["value"] == "SN-AC1000-8841"


def test_unit_observe_real_mode_recovers_from_malformed_output_via_repair(monkeypatch, tmp_path):
    """Same JSON-repair-at-the-stage-boundary guarantee as vision_identify: a
    non-JSON model response must not crash the stage or fabricate a value."""
    import json

    from resale_listing_ai import adapters, json_repair

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    fake_openai = _FakeOpenAIClient("Looks lightly used, minor scuffing, powers on fine.")
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_openai)

    repaired = {f: {"value": None, "confidence": 0.0, "source": "vision"} for f in adapters.UNIT_FIELDS}
    repaired["VisibleWear"] = {"value": "Light scuffing", "confidence": 0.6, "source": "vision"}
    fake_anthropic = _FakeAnthropicClient(json.dumps(repaired))
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_anthropic)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    unit = adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": None})

    assert unit["VisibleWear"]["value"] == "Light scuffing"
    assert unit["TestingStatus"]["value"] is None  # not recovered -> stays null, never guessed


def test_unit_observe_requires_api_key_when_not_stubbed(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        adapters.unit_observe({"submission_id": "x", "images": [], "notes": None})


def test_unit_observe_dispatches_to_gemini_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    calls = {}

    def fake_vision_json(instructions, notes, images):
        calls["notes"] = notes
        return json.dumps(RAW_UNIT_OUTPUT)

    monkeypatch.setattr(gemini, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    unit = adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": "note"})

    assert calls["notes"] == "note"
    assert unit["SerialNumber"]["value"] == "SN-AC1000-8841"


def test_unit_observe_dispatches_to_openrouter_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    monkeypatch.setattr(
        openrouter, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_UNIT_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    unit = adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": None})

    assert unit["SerialNumber"]["value"] == "SN-AC1000-8841"


def test_unit_observe_gemini_requires_api_key_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    with pytest.raises(RuntimeError):
        adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": None})


def test_unit_observe_gemini_prompt_names_the_real_schema_fields(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    seen = {}

    def fake_vision_json(instructions, notes, images):
        seen["instructions"] = instructions
        return json.dumps(RAW_UNIT_OUTPUT)

    monkeypatch.setattr(gemini, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": None})

    instructions = seen["instructions"]
    assert "never guess" in instructions
    for field in ("SerialNumber", "AISuggestedConditionGrade", "TestingStatus", "UnitSpecificNotes"):
        assert field in instructions
    for key in ("value", "confidence", "source"):
        assert key in instructions
    for field in adapters.UNIT_JSON_SCHEMA["required"]:
        assert field in instructions


def test_unit_observe_instructions_require_context_source_for_testing_fields():
    """A photo cannot show whether a device powers on -- TestingStatus/
    TestsPerformed/TestResults must come only from submitter notes (source
    'context'), never be inferred from cosmetic appearance. This is the one
    place this adapter must be stricter than vision_identify."""
    from resale_listing_ai import adapters

    instructions = adapters.UNIT_INSTRUCTIONS

    for field in ("TestingStatus", "TestsPerformed", "TestResults"):
        assert field in instructions
    assert "context" in instructions
    assert "never" in instructions.lower()


def test_unit_observe_accepts_an_optional_ledger():
    import inspect

    from resale_listing_ai import adapters

    params = inspect.signature(adapters.unit_observe).parameters
    assert params["ledger"].default is None


def test_unit_observe_stub_mode_logs_nothing(monkeypatch):
    """Unlike vision_identify, the stub path never logged a cost row for
    unit_observe historically -- that behavior must not change."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    ledger = CostLedger()
    unit = adapters.unit_observe({"submission_id": "x", "hint": "nest-doorbell"}, ledger=ledger)

    assert ledger.rows == []
    assert unit["SerialNumber"]["value"] == "SN-AC1000-8841"


def test_unit_observe_logs_gemini_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(
        gemini, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_UNIT_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.unit_observe(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["stage"] == "identify"
    assert ledger.rows[0]["service"] == "gemini_vision_observe"
    assert ledger.rows[0]["cost_cad"] == gemini.GEMINI_UNIT_COST_CAD


def test_unit_observe_dispatches_to_bedrock_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    calls = {}

    def fake_vision_json(instructions, notes, images):
        calls["notes"] = notes
        return json.dumps(RAW_UNIT_OUTPUT)

    monkeypatch.setattr(bedrock, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    unit = adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": "note"})

    assert calls["notes"] == "note"
    assert unit["SerialNumber"]["value"] == "SN-AC1000-8841"


def test_unit_observe_bedrock_requires_aws_credentials_when_selected(monkeypatch, tmp_path):
    import boto3

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    class _FakeSession:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", _FakeSession)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    with pytest.raises(RuntimeError):
        adapters.unit_observe({"submission_id": "x", "images": [str(img)], "notes": None})


def test_unit_observe_logs_bedrock_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    monkeypatch.setattr(
        bedrock, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_UNIT_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.unit_observe(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["stage"] == "identify"
    assert ledger.rows[0]["service"] == "bedrock_vision_observe"
    assert ledger.rows[0]["cost_cad"] == bedrock.BEDROCK_UNIT_COST_CAD


def test_unit_observe_logs_openrouter_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    monkeypatch.setattr(
        openrouter, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_UNIT_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.unit_observe(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["service"] == "openrouter_vision_observe"
    assert ledger.rows[0]["cost_cad"] == openrouter.OPENROUTER_UNIT_COST_CAD


def test_unit_observe_logs_openai_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)

    class _FakeResponses:
        def create(self, **kwargs):
            return type("R", (), {"output_text": json.dumps(RAW_UNIT_OUTPUT)})()

    class _FakeClient:
        def __init__(self):
            self.responses = _FakeResponses()

    monkeypatch.setattr(adapters, "_get_openai_client", lambda: _FakeClient())

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.unit_observe(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["service"] == "openai_vision_observe"
    assert ledger.rows[0]["cost_cad"] == adapters.VISION_UNIT_COST_CAD


# ---------------------------------------------------------------------------
# Web-search provider selection (config/provider_stack.json + env override)
# ---------------------------------------------------------------------------

def test_web_search_provider_defaults_to_gemini_search(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", raising=False)

    assert adapters._web_search_provider() == "gemini_search"


def test_web_search_provider_overridable_via_env(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "perplexity_sonar")

    assert adapters._web_search_provider() == "perplexity_sonar"


def test_web_search_provider_uses_admin_override(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", raising=False)
    assert adapters._web_search_provider(
        config={"provider_stack.web_search_provider": "perplexity_sonar"}) == "perplexity_sonar"


# ---------------------------------------------------------------------------
# _call_with_cost — retry-aware cost-ledger logging (pure, no network)
# ---------------------------------------------------------------------------

class _FlakyTransientError(Exception):
    pass


def test_call_with_cost_logs_the_successful_call_once():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    result = adapters._call_with_cost(
        ledger, "sub-1", "pricing", "openai_web_search", 0.02, lambda: "ok",
        retry_exceptions=(_FlakyTransientError,),
    )

    assert result == "ok"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["retry"] is False
    assert ledger.rows[0]["stage"] == "pricing"
    assert ledger.rows[0]["service"] == "openai_web_search"
    assert ledger.rows[0]["submission_id"] == "sub-1"


def test_call_with_cost_logs_each_retry_then_the_success():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FlakyTransientError("transient")
        return "ok"

    ledger = CostLedger()
    result = adapters._call_with_cost(
        ledger, "sub-1", "pricing", "openai_web_search", 0.02, flaky,
        retry_exceptions=(_FlakyTransientError,), max_retries=2, sleep=lambda s: None,
    )

    assert result == "ok"
    assert [r["retry"] for r in ledger.rows] == [False, True, True]


def test_call_with_cost_reraises_after_exhausting_retries():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    def always_fails():
        raise _FlakyTransientError("still down")

    ledger = CostLedger()
    with pytest.raises(_FlakyTransientError):
        adapters._call_with_cost(
            ledger, "sub-1", "pricing", "openai_web_search", 0.02, always_fails,
            retry_exceptions=(_FlakyTransientError,), max_retries=2, sleep=lambda s: None,
        )
    assert len(ledger.rows) == 3  # original attempt + 2 retries, every one logged


def test_responses_usage_extracts_input_and_output_tokens():
    from types import SimpleNamespace
    from resale_listing_ai import adapters

    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1200, output_tokens=340))
    assert adapters._responses_usage(response, "gpt-4o-mini") == {
        "model": "gpt-4o-mini", "input_tokens": 1200, "output_tokens": 340}


def test_responses_usage_returns_none_when_response_has_no_usage():
    from resale_listing_ai import adapters

    class _NoUsage:
        output_text = "hi"

    assert adapters._responses_usage(_NoUsage(), "gpt-4o-mini") is None


def test_call_with_cost_prices_from_token_usage_when_fn_returns_usage():
    """fn may return (text, usage); a priceable model bills the real token
    cost (config/model_rates.json gpt-4o-mini) and the row records tokens+model."""
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    usage = {"model": "gpt-4o-mini", "input_tokens": 1000, "output_tokens": 500}
    result = adapters._call_with_cost(
        ledger, "sub-1", "identify", "openai_vision", 0.030,
        lambda: ("the text", usage),
    )

    assert result == "the text"  # caller still gets the text, not the tuple
    row = ledger.rows[0]
    assert row["cost_cad"] == 0.0006  # token cost (0.000621) stored at ledger 4dp
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["model"] == "gpt-4o-mini"


def test_call_with_cost_falls_back_to_flat_cost_for_unpriced_model_but_keeps_tokens():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    usage = {"model": "mystery-model", "input_tokens": 1000, "output_tokens": 500}
    result = adapters._call_with_cost(
        ledger, "sub-1", "identify", "openai_vision", 0.030,
        lambda: ("txt", usage),
    )

    assert result == "txt"
    row = ledger.rows[0]
    assert row["cost_cad"] == 0.030  # unpriced model -> flat unit cost
    assert row["input_tokens"] == 1000  # tokens still recorded for audit
    assert row["model"] == "mystery-model"


def test_call_with_cost_bare_string_return_stays_flat_with_no_token_detail():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    result = adapters._call_with_cost(
        ledger, "sub-1", "pricing", "openai_web_search", 0.02, lambda: "ok",
    )

    assert result == "ok"
    row = ledger.rows[0]
    assert row["cost_cad"] == 0.02
    assert row["input_tokens"] is None
    assert row["model"] is None


# ---------------------------------------------------------------------------
# Pure output-mapping functions — retail_price / resale_price / image_match
# ---------------------------------------------------------------------------

def test_map_retail_price_output_retains_cad_price_and_source_urls():
    from resale_listing_ai import adapters

    raw = {
        "match_found": True,
        "anchor": {
            "price_cad": 149.99,
            "confidence": 0.74,
            "source_name": "The Home Depot Canada",
            "source_url": "https://www.homedepot.ca/product/123",
            "original_currency": "CAD",
        },
        "corroborating": {
            "price_cad": 159.99,
            "source_name": "Best Buy Canada",
            "source_url": "https://www.bestbuy.ca/product/456",
        },
        "confidence_label": "Medium",
    }

    mapped = adapters._map_retail_price_output(raw, "2026-07-31")

    assert mapped["PricingAnchorPriceCAD"] == {"value": 149.99, "confidence": 0.74, "source": "external_lookup"}
    assert mapped["PricingAnchorSourceURL"]["value"] == "https://www.homedepot.ca/product/123"
    assert mapped["CorroboratingSourcePriceCAD"]["value"] == 159.99
    assert mapped["CorroboratingSourceURL"]["value"] == "https://www.bestbuy.ca/product/456"
    assert mapped["PricingConfidence"]["value"] == "Medium"
    assert mapped["LastPricingCheckDate"] == {"value": "2026-07-31", "confidence": 1.0, "source": "derived"}


def test_map_retail_price_output_converts_usd_source_when_model_reports_it():
    # PROVIDER_STACK.md: the model is instructed to always return price_cad already
    # converted; original_currency is retained purely as an audit note.
    from resale_listing_ai import adapters

    raw = {
        "match_found": True,
        "anchor": {
            "price_cad": 205.49,
            "confidence": 0.65,
            "source_name": "Example US Retailer",
            "source_url": "https://example.com/product",
            "original_currency": "USD",
        },
        "corroborating": None,
        "confidence_label": "Low",
    }

    mapped = adapters._map_retail_price_output(raw, "2026-07-31")

    assert mapped["PricingAnchorPriceCAD"]["value"] == 205.49
    assert mapped["CorroboratingSourcePriceCAD"] == {"value": None, "confidence": 0.0, "source": "none"}


def test_map_retail_price_output_no_match_returns_null_not_fabricated():
    from resale_listing_ai import adapters

    raw = {"match_found": False, "anchor": None, "corroborating": None, "confidence_label": "Low"}

    mapped = adapters._map_retail_price_output(raw, "2026-07-31")

    for field in ("PricingAnchorPriceCAD", "PricingAnchorSourceName", "PricingAnchorSourceURL",
                  "CorroboratingSourcePriceCAD", "CorroboratingSourceName", "CorroboratingSourceURL",
                  "PricingConfidence"):
        assert mapped[field] == {"value": None, "confidence": 0.0, "source": "none"}
    assert mapped["LastPricingCheckDate"]["value"] == "2026-07-31"  # the check still happened


def test_map_resale_price_output_maps_comparable_fields_only():
    from resale_listing_ai import adapters

    raw = {
        "match_found": True,
        "comparable": {
            "brand": "ecobee",
            "product_name": "ecobee Smart Doorbell Camera (wired)",
            "price_cad": 95.00,
            "confidence": 0.55,
            "source_url": "https://www.ebay.ca/itm/123",
        },
    }

    mapped = adapters._map_resale_price_output(raw)

    assert set(mapped) == {"ComparableBrand", "ComparableProductName", "ComparableProductPriceCAD", "ComparableProductURL"}
    assert mapped["ComparableProductPriceCAD"]["value"] == 95.00
    assert mapped["ComparableProductURL"]["value"] == "https://www.ebay.ca/itm/123"


def test_map_resale_price_output_no_match_returns_empty_not_fabricated():
    from resale_listing_ai import adapters

    mapped = adapters._map_resale_price_output({"match_found": False, "comparable": None})

    assert mapped == {}


def test_resale_price_prompt_config_matches_the_loaded_schema():
    """Literal expected values, not a reload-and-compare (see the note on
    test_image_classify_prompt_config_matches_the_loaded_constants)."""
    from resale_listing_ai import adapters

    assert adapters.RESALE_PRICE_SCHEMA["required"] == ["match_found", "comparable"]
    assert "{query}" in adapters._RESALE_PRICE_TEMPLATE
    assert "never estimate or guess a price" in adapters._RESALE_PRICE_TEMPLATE


def test_map_image_match_output_confirms_with_external_lookup_source():
    from resale_listing_ai import adapters

    mapped = adapters._map_image_match_output(
        {"matched": True, "confidence": 0.88, "match_description": "Acme Smart Doorbell, wired 2nd gen"})

    assert mapped == {"matched": True, "confidence": 0.88, "source": "external_lookup"}


def test_map_image_match_output_no_match_returns_null_shape_not_fabricated():
    from resale_listing_ai import adapters

    mapped = adapters._map_image_match_output({"matched": False, "confidence": 0.0, "match_description": None})

    assert mapped == {"matched": False, "confidence": 0.0, "source": "none"}


# ---------------------------------------------------------------------------
# image_match / retail_price / resale_price — real mode, mocked OpenAI client
# ---------------------------------------------------------------------------

SOME_IDENTITY = {
    "Brand": {"value": "Acme", "confidence": 0.9, "source": "vision"},
    "ProductName": {"value": "Smart Doorbell (wired, 2nd gen)", "confidence": 0.9, "source": "vision"},
    "ModelNumber": {"value": "AC1000-XY", "confidence": 0.9, "source": "label_ocr"},
}


def test_retail_price_real_mode_cad_and_source_url_retained(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openai_web_search")  # this test mocks the openai client
    raw = {
        "match_found": True,
        "anchor": {"price_cad": 149.99, "confidence": 0.74, "source_name": "The Home Depot Canada",
                    "source_url": "https://www.homedepot.ca/product/123", "original_currency": "CAD"},
        "corroborating": None,
        "confidence_label": "Medium",
    }
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    result = adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result["PricingAnchorPriceCAD"]["value"] == 149.99
    assert result["PricingAnchorSourceURL"]["value"] == "https://www.homedepot.ca/product/123"
    assert fake_client.responses.last_call["tools"] == [{"type": "web_search"}]


def test_retail_price_no_identity_or_notes_short_circuits_without_calling_api(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)

    def _boom():
        raise AssertionError("must not call the API with nothing to search for")

    monkeypatch.setattr(adapters, "_get_openai_client", _boom)

    result = adapters.retail_price({"submission_id": "x", "notes": None}, None)

    assert result["PricingAnchorPriceCAD"]["value"] is None


def test_resale_price_real_mode_maps_comparable_fields(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openai_web_search")  # this test mocks the openai client
    raw = {"match_found": True, "comparable": {
        "brand": "ecobee", "product_name": "ecobee Smart Doorbell Camera (wired)",
        "price_cad": 95.00, "confidence": 0.55, "source_url": "https://www.ebay.ca/itm/123"}}
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    result = adapters.resale_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result["ComparableProductPriceCAD"]["value"] == 95.00
    assert "PricingAnchorPriceCAD" not in result  # never the anchor


def test_image_match_real_mode_matched(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openai_web_search")  # this test mocks the openai client
    raw = {"matched": True, "confidence": 0.88, "match_description": "Acme Smart Doorbell, wired"}
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    result = adapters.image_match({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result == {"matched": True, "confidence": 0.88, "source": "external_lookup"}


def test_image_match_real_mode_no_confident_identity_returns_no_match_without_calling_api(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)

    def _boom():
        raise AssertionError("must not call the API with nothing to confirm")

    monkeypatch.setattr(adapters, "_get_openai_client", _boom)

    result = adapters.image_match({"submission_id": "x", "notes": None}, None)

    assert result == {"matched": False, "confidence": 0.0, "source": "none"}


def test_image_match_prompt_config_matches_the_loaded_schema():
    """Literal expected values, not a reload-and-compare (see the note on
    test_image_classify_prompt_config_matches_the_loaded_constants -- same
    reasoning: no assembly step here to legitimately verify)."""
    from resale_listing_ai import adapters

    assert adapters.IMAGE_MATCH_SCHEMA["required"] == ["matched", "confidence", "match_description"]
    assert "{query}" in adapters._IMAGE_MATCH_TEMPLATE
    assert "Never invent a match" in adapters._IMAGE_MATCH_TEMPLATE


def test_image_match_survives_a_template_with_incidental_braces(monkeypatch):
    """.replace(), not str.format() -- the real risk is a stray {...} in the
    INSTRUCTIONS TEMPLATE itself (e.g. a future prompt edit that adds a JSON
    example alongside the {query} placeholder), not in the query value.
    str.format(query=query) never re-parses the substituted query for further
    {} fields, so a brace in the query alone can't distinguish the two -- this
    test instead puts the stray brace in the template, where .format() would
    raise KeyError and .replace() would not."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setattr(
        adapters, "_IMAGE_MATCH_TEMPLATE",
        'Match this product: "{query}". Example bad output to avoid: {"matched": true}.')
    seen = {}

    def fake_web_search_json(instructions, schema, schema_name, *, ledger, submission_id, stage, config=None):
        seen["instructions"] = instructions
        return '{"matched": false, "confidence": 0.0, "match_description": null}'

    monkeypatch.setattr(adapters, "_web_search_json", fake_web_search_json)

    adapters.image_match({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert "Smart Doorbell" in seen["instructions"]
    assert 'Example bad output to avoid: {"matched": true}' in seen["instructions"]


def test_retail_price_logs_to_cost_ledger(monkeypatch):
    import json

    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openai_web_search")  # this test mocks the openai client
    raw = {"match_found": False, "anchor": None, "corroborating": None, "confidence_label": "Low"}
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    ledger = CostLedger()
    adapters.retail_price({"submission_id": "sub-9", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert len(ledger.rows) == 1
    assert ledger.rows[0]["submission_id"] == "sub-9"
    assert ledger.rows[0]["stage"] == "pricing"
    assert ledger.rows[0]["service"] == "openai_web_search"


def test_tier2_names_are_removed():
    from resale_listing_ai import adapters
    for name in ("_manufacturer_hero_image", "_hero_verify",
                 "_map_hero_image_output", "_map_hero_verify_output"):
        assert not hasattr(adapters, name), name


def test_tier2_prompt_files_are_gone():
    from pathlib import Path
    import resale_listing_ai
    prompts = Path(resale_listing_ai.__file__).resolve().parent.parent / "config" / "prompts"
    assert not (prompts / "hero_image.json").exists()
    assert not (prompts / "hero_verify.json").exists()


def test_hero_qc_prompt_config_loaded():
    from resale_listing_ai import adapters

    assert adapters.HERO_QC_SCHEMA["required"] == [
        "branding_clear", "coherent", "matches_confirmed", "no_invented", "reasons"]
    assert "{query}" in adapters._HERO_QC_TEMPLATE
    assert "{depiction}" in adapters._HERO_GENERATE_TEMPLATE
    assert "{identification}" in adapters._HERO_GENERATE_TEMPLATE


# ---------------------------------------------------------------------------
# Perplexity Sonar — alternate provider selection
# ---------------------------------------------------------------------------

class _FakePerplexityMessage:
    def __init__(self, content):
        self.content = content


class _FakePerplexityChoice:
    def __init__(self, content):
        self.message = _FakePerplexityMessage(content)


class _FakePerplexityCompletionResponse:
    def __init__(self, content):
        self.choices = [_FakePerplexityChoice(content)]


class _FakePerplexityCompletions:
    def __init__(self, content):
        self._content = content
        self.last_call = None

    def create(self, **kwargs):
        self.last_call = kwargs
        return _FakePerplexityCompletionResponse(self._content)


class _FakePerplexityClient:
    def __init__(self, content):
        self.chat = type("chat", (), {})()
        self.chat.completions = _FakePerplexityCompletions(content)


def test_retail_price_uses_perplexity_when_configured(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "perplexity_sonar")
    raw = {
        "match_found": True,
        "anchor": {"price_cad": 149.99, "confidence": 0.74, "source_name": "The Home Depot Canada",
                    "source_url": "https://www.homedepot.ca/product/123", "original_currency": "CAD"},
        "corroborating": None,
        "confidence_label": "Medium",
    }
    fake_client = _FakePerplexityClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_perplexity_client", lambda: fake_client)

    result = adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result["PricingAnchorPriceCAD"]["value"] == 149.99
    assert fake_client.chat.completions.last_call is not None


def test_perplexity_requires_api_key_when_selected_and_not_stubbed(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "perplexity_sonar")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

# ---------------------------------------------------------------------------
# _connect_storage / _put_object / _folder_url — S3-vs-local auto-detection
# ---------------------------------------------------------------------------

def test_connect_storage_falls_back_to_local_when_s3_client_construction_fails(monkeypatch, capsys):
    from resale_listing_ai import adapters

    def raise_client(config=None):
        raise Exception("no AWS credentials found")

    monkeypatch.setattr(adapters, "_get_s3_client", raise_client)

    storage = adapters._connect_storage()

    assert storage["mode"] == "local"
    assert storage["client"] is None
    # Finding 1: the fallback must not be silent -- an operator-facing message
    # is printed naming the underlying exception.
    out = capsys.readouterr().out
    assert "S3 unavailable" in out
    assert "no AWS credentials found" in out


def test_connect_storage_falls_back_to_local_when_bucket_unreachable(monkeypatch, capsys):
    from resale_listing_ai import adapters

    class FakeClient:
        def head_bucket(self, Bucket):
            raise Exception("403 Forbidden")

    monkeypatch.setattr(adapters, "_get_s3_client", lambda config=None: FakeClient())

    storage = adapters._connect_storage()

    assert storage["mode"] == "local"
    assert storage["client"] is None
    out = capsys.readouterr().out
    assert "S3 unavailable" in out
    assert "403 Forbidden" in out


def test_connect_storage_probes_with_a_short_timeout_no_retry_client(monkeypatch):
    """Finding 2: the head_bucket probe must use its own short-timeout,
    no-retry client so an unreachable network fails fast instead of
    blocking for minutes on botocore's default connect/retry behavior."""
    from resale_listing_ai import adapters

    seen_configs = []

    class FakeClient:
        def head_bucket(self, Bucket):
            return {}

    def fake_get_s3_client(config=None):
        seen_configs.append(config)
        return FakeClient()

    monkeypatch.setattr(adapters, "_get_s3_client", fake_get_s3_client)

    storage = adapters._connect_storage()

    assert storage["mode"] == "s3"
    # First call (the probe) passes a real botocore Config with a short
    # timeout and no retries; second call (the real client) passes none.
    assert len(seen_configs) == 2
    probe_config, real_config = seen_configs
    assert probe_config is not None
    assert probe_config.connect_timeout <= 2
    assert probe_config.read_timeout <= 2
    assert probe_config.retries["max_attempts"] == 1
    assert real_config is None


def test_connect_storage_returns_s3_mode_when_bucket_reachable(monkeypatch):
    from resale_listing_ai import adapters

    class FakeClient:
        def head_bucket(self, Bucket):
            assert Bucket == adapters.S3_BUCKET
            return {}

    fake_client = FakeClient()
    monkeypatch.setattr(adapters, "_get_s3_client", lambda config=None: fake_client)

    storage = adapters._connect_storage()

    assert storage == {"mode": "s3", "client": fake_client}


def test_put_object_local_mode_writes_file_and_returns_file_url(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_LOCAL_IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(adapters, "LOCAL_IMAGE_DIR", str(tmp_path))
    storage = {"mode": "local", "client": None}

    url = adapters._put_object(storage, "originals/sub-1/photo.jpg", b"raw-bytes")

    written = tmp_path / "originals" / "sub-1" / "photo.jpg"
    assert written.read_bytes() == b"raw-bytes"
    assert url == f"file://{written.resolve()}"


def test_put_object_s3_mode_delegates_to_s3_put(monkeypatch):
    from resale_listing_ai import adapters

    calls = []

    def fake_s3_put(client, key, data, content_type="image/jpeg"):
        calls.append((client, key, data, content_type))
        return f"s3://test-bucket/{key}"

    monkeypatch.setattr(adapters, "_s3_put", fake_s3_put)
    storage = {"mode": "s3", "client": "fake-client"}

    url = adapters._put_object(storage, "originals/sub-1/photo.jpg", b"raw-bytes")

    assert url == "s3://test-bucket/originals/sub-1/photo.jpg"
    assert calls == [("fake-client", "originals/sub-1/photo.jpg", b"raw-bytes", "image/jpeg")]


def test_folder_url_local_mode(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.setattr(adapters, "LOCAL_IMAGE_DIR", str(tmp_path))
    storage = {"mode": "local", "client": None}

    url = adapters._folder_url(storage, "originals/sub-1/")

    assert url == f"file://{(tmp_path / 'originals' / 'sub-1').resolve()}/"


def test_folder_url_s3_mode(monkeypatch):
    from resale_listing_ai import adapters

    storage = {"mode": "s3", "client": None}

    url = adapters._folder_url(storage, "originals/sub-1/")

    assert url == f"s3://{adapters.S3_BUCKET}/originals/sub-1/"


# ---------------------------------------------------------------------------
# image_process — pure helpers (classification mapping, availability flags,
# finished-image selection, cleanup mask)
# ---------------------------------------------------------------------------

def test_map_classify_output_maps_known_category_and_cleanup_flag():
    from resale_listing_ai import adapters

    mapped = adapters._map_classify_output(
        {"category": "actual_product", "needs_cleanup": True,
         "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}})

    assert mapped["category"] == "actual_product"
    assert mapped["needs_cleanup"] is True
    assert mapped["clutter_bbox"] == {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}


def test_map_classify_output_unknown_category_falls_back_to_contextual():
    from resale_listing_ai import adapters

    mapped = adapters._map_classify_output({"category": "spaceship", "needs_cleanup": False})

    assert mapped["category"] == "contextual"
    assert mapped["clutter_bbox"] is None


def test_map_classify_output_malformed_raw_never_raises():
    from resale_listing_ai import adapters

    mapped = adapters._map_classify_output(None)

    assert mapped == {"category": "contextual", "needs_cleanup": False, "clutter_bbox": None}


def test_map_hero_qc_output_coerces_booleans_and_reasons():
    from resale_listing_ai import adapters

    out = adapters._map_hero_qc_output({
        "branding_clear": True, "coherent": True,
        "matches_confirmed": False, "no_invented": True,
        "reasons": ["wrong colour"]})
    assert out == {"branding_clear": True, "coherent": True,
                   "matches_confirmed": False, "no_invented": True,
                   "reasons": ["wrong colour"]}


def test_map_hero_qc_output_none_for_non_dict():
    from resale_listing_ai import adapters
    assert adapters._map_hero_qc_output(None) is None


def test_map_hero_qc_output_coerces_truthy_non_bool_values():
    """Coercion via bool() must handle non-bool truthy/falsy model output
    (e.g. 1/0, "true"/""), and a missing or non-list reasons must map to []."""
    from resale_listing_ai import adapters

    out = adapters._map_hero_qc_output({
        "branding_clear": 1, "coherent": 0,
        "matches_confirmed": "yes", "no_invented": "",
        "reasons": "not-a-list"})
    assert out == {"branding_clear": True, "coherent": False,
                   "matches_confirmed": True, "no_invented": False,
                   "reasons": []}


def test_photo_availability_flags_yes_only_for_categories_present():
    from resale_listing_ai import adapters

    classified = [
        ("a.jpg", {"category": "actual_product", "needs_cleanup": False}),
        ("b.jpg", {"category": "product_information_label", "needs_cleanup": False}),
    ]

    flags = adapters._photo_availability_flags(classified)

    assert flags["ActualProductPhotosAvailable"]["value"] == "Yes"
    assert flags["ProductInformationLabelPhotosAvailable"]["value"] == "Yes"
    assert flags["OriginalPackagingPhotosAvailable"]["value"] == "No"
    assert flags["ContextualProductPhotosAvailable"]["value"] == "No"
    assert set(flags) == {"ActualProductPhotosAvailable", "OriginalPackagingPhotosAvailable",
                           "ProductInformationLabelPhotosAvailable", "ContextualProductPhotosAvailable"}


def test_photo_availability_flags_no_images_all_no():
    from resale_listing_ai import adapters

    flags = adapters._photo_availability_flags([])

    assert all(f["value"] == "No" for f in flags.values())
    assert all(f["source"] == "derived" for f in flags.values())


def test_select_finished_images_prefers_actual_product_category():
    from resale_listing_ai import adapters

    classified = [
        ("box.jpg", {"category": "original_packaging", "needs_cleanup": False}),
        ("unit1.jpg", {"category": "actual_product", "needs_cleanup": False}),
        ("unit2.jpg", {"category": "actual_product", "needs_cleanup": False}),
    ]

    chosen = adapters._select_finished_images(classified)

    assert chosen == ["unit1.jpg", "unit2.jpg"]


def test_select_finished_images_caps_at_one_hero_plus_three_supporting():
    from resale_listing_ai import adapters

    classified = [(f"u{i}.jpg", {"category": "actual_product", "needs_cleanup": False}) for i in range(6)]

    chosen = adapters._select_finished_images(classified)

    assert len(chosen) == 4  # 1 hero + at most 3 supporting


def test_select_finished_images_falls_back_to_any_image_when_none_are_actual_product():
    from resale_listing_ai import adapters

    classified = [("box.jpg", {"category": "original_packaging", "needs_cleanup": False})]

    chosen = adapters._select_finished_images(classified)

    assert chosen == ["box.jpg"]


def test_validate_bbox_rejects_missing_and_out_of_range():
    from resale_listing_ai import adapters

    assert adapters._validate_bbox(None) is None
    assert adapters._validate_bbox({"x": 1.5, "y": 0, "w": 0.1, "h": 0.1}) is None
    assert adapters._validate_bbox({"x": 0, "y": 0, "w": 0, "h": 0.1}) is None
    assert adapters._validate_bbox({"x": "nope", "y": 0, "w": 0.1, "h": 0.1}) is None


def test_validate_bbox_returns_fractions_for_a_valid_bbox():
    from resale_listing_ai import adapters

    assert adapters._validate_bbox({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}) == (0.1, 0.2, 0.3, 0.4)


def test_bbox_px_converts_fractions_to_pixel_rectangle_clamped_to_image_bounds():
    from resale_listing_ai import adapters

    # a bbox starting near the edge must clamp to the image size, never overshoot
    assert adapters._bbox_px(0.0, 0.0, 0.5, 0.5, 100, 100) == (0, 0, 50, 50)
    assert adapters._bbox_px(0.9, 0.9, 0.5, 0.5, 100, 100) == (90, 90, 100, 100)


def test_build_cleanup_mask_none_without_a_usable_bbox():
    from resale_listing_ai import adapters

    assert adapters._build_cleanup_mask(b"not-really-an-image", None) is None
    assert adapters._build_cleanup_mask(b"not-really-an-image", {"x": 1.5, "y": 0, "w": 0.1, "h": 0.1}) is None


def test_build_cleanup_mask_covers_only_the_reported_region():
    from io import BytesIO

    from PIL import Image as PILImage

    from resale_listing_ai import adapters

    src = PILImage.new("RGB", (100, 100), "white")
    buf = BytesIO()
    src.save(buf, format="PNG")

    mask_bytes = adapters._build_cleanup_mask(buf.getvalue(), {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5})

    mask = PILImage.open(BytesIO(mask_bytes))
    assert mask.size == (100, 100)
    assert mask.getpixel((10, 10)) == 255   # inside the flagged region -> masked for cleanup
    assert mask.getpixel((90, 90)) == 0     # outside -> untouched, protects real defect evidence there


def test_draw_bbox_marker_none_without_a_usable_bbox():
    from resale_listing_ai import adapters

    assert adapters._draw_bbox_marker(b"not-really-an-image", None) is None
    assert adapters._draw_bbox_marker(b"not-really-an-image", {"x": 1.5, "y": 0, "w": 0.1, "h": 0.1}) is None


def test_draw_bbox_marker_draws_a_rectangle_at_the_reported_region_only():
    from io import BytesIO

    from PIL import Image as PILImage

    from resale_listing_ai import adapters

    src = PILImage.new("RGB", (100, 100), "white")
    buf = BytesIO()
    src.save(buf, format="PNG")

    marked_bytes = adapters._draw_bbox_marker(buf.getvalue(), {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3})

    marked = PILImage.open(BytesIO(marked_bytes)).convert("RGB")
    assert marked.size == (100, 100)
    # the outline sits at the rectangle's edge (left=10, top=10) -> that pixel is
    # no longer plain white
    assert marked.getpixel((10, 10)) != (255, 255, 255)
    # interior, well inside the outline's line width -> untouched, proves it's an
    # outline (not a filled region like Clipdrop's mask)
    assert marked.getpixel((25, 25)) == (255, 255, 255)
    # far outside the flagged region -> untouched, exactly white
    assert marked.getpixel((90, 90)) == (255, 255, 255)


# ---------------------------------------------------------------------------
# image_process — real mode, mocked classify/PhotoRoom/Clipdrop/S3 collaborators
# ---------------------------------------------------------------------------

def _png_bytes():
    from io import BytesIO

    from PIL import Image as PILImage

    buf = BytesIO()
    PILImage.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def _make_images(tmp_path, names):
    paths = []
    for name in names:
        p = tmp_path / name
        p.write_bytes(b"raw-bytes-for-" + name.encode())
        paths.append(str(p))
    return paths


def _install_image_process_fakes(monkeypatch, *, classify_by_name, cleanup_calls):
    from resale_listing_ai import adapters

    def fake_classify(path, *, ledger, submission_id, config=None):
        if ledger is not None:
            ledger.add(submission_id, "images", "openai_vision_classify", 0.01)
        return adapters._map_classify_output(classify_by_name[Path(path).name])

    def fake_photoroom(image_bytes, *, ledger, submission_id):
        if ledger is not None:
            ledger.add(submission_id, "images", "photoroom", 0.02)
        return _png_bytes()

    def fake_clipdrop(image_bytes, mask_bytes, *, ledger, submission_id):
        cleanup_calls.append(image_bytes)
        if ledger is not None:
            ledger.add(submission_id, "images", "clipdrop_cleanup", 0.05)
        return b"cleaned:" + image_bytes

    def fake_s3_put(client, key, data, content_type="image/jpeg"):
        return f"s3://test-bucket/{key}"

    # image_process gates the Clipdrop-cleanup branch on CLIPDROP_API_KEY being
    # set; pin a dummy so the (mocked) cleanup is reached deterministically rather
    # than depending on an ambient key from a real .env.
    monkeypatch.setenv("CLIPDROP_API_KEY", "test-clipdrop-key")
    monkeypatch.setattr(adapters, "_classify_image", fake_classify)
    monkeypatch.setattr(adapters, "_photoroom_remove_background", fake_photoroom)
    monkeypatch.setattr(adapters, "_clipdrop_cleanup", fake_clipdrop)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: object())
    monkeypatch.setattr(adapters, "_s3_put", fake_s3_put)
    monkeypatch.setattr(adapters, "_connect_storage", lambda: {"mode": "s3", "client": object()})


def test_image_process_stub_returns_website_hero_keys(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    from resale_listing_ai import adapters
    submission = {"submission_id": "s1", "images": ["/tmp/a.jpg"]}
    out = adapters.image_process(submission, "PK")
    assert "website_hero" in out
    assert "website_hero_review" in out


def test_image_process_real_mode_returns_one_hero_and_at_most_three_supporting(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = [f"unit{i}.jpg" for i in range(5)]
    images = _make_images(tmp_path, names)
    classify_by_name = {name: {"category": "actual_product", "needs_cleanup": False} for name in names}
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    result = adapters.image_process({"submission_id": "sub-1", "images": images}, "PRODKEY")

    assert result["hero"]["value"] is not None
    assert 0 <= len(result["supporting"]) <= 3
    assert cleanup_calls == []  # nothing flagged -> Clipdrop never called


def test_image_process_real_mode_only_cleans_flagged_images_never_others(monkeypatch, tmp_path):
    """Only the image the classifier flags gets sent to Clipdrop cleanup; every other
    chosen image is background-removed only, so any real defect evidence it shows is
    architecturally never touched by the inpainting call."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["clean.jpg", "has_tape.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "clean.jpg": {"category": "actual_product", "needs_cleanup": False},
        "has_tape.jpg": {"category": "actual_product", "needs_cleanup": True,
                          "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    result = adapters.image_process({"submission_id": "sub-2", "images": images}, "PRODKEY")

    assert len(cleanup_calls) == 1  # exactly one image needed cleanup
    finished_urls = [result["hero"]["value"]] + [s["value"] for s in result["supporting"]]
    assert any("has_tape.jpg" in u for u in finished_urls)
    assert any("clean.jpg" in u for u in finished_urls)


def test_image_process_real_mode_flagged_without_bbox_skips_cleanup(monkeypatch, tmp_path):
    """needs_cleanup with no usable bbox must NOT fall back to whole-image inpainting —
    that would risk smoothing away real defect evidence outside the flagged clutter."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["mystery_clutter.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"mystery_clutter.jpg": {"category": "actual_product", "needs_cleanup": True,
                                                 "clutter_bbox": None}}
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    adapters.image_process({"submission_id": "sub-3", "images": images}, "PRODKEY")

    assert cleanup_calls == []


def test_image_process_real_mode_sets_availability_flags_from_all_submitted_images(monkeypatch, tmp_path):
    """A packaging photo that isn't chosen for the finished set must still count toward
    OriginalPackagingPhotosAvailable — the flags reflect everything submitted, not just
    what became a hero/supporting image."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["unit.jpg", "box.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "unit.jpg": {"category": "actual_product", "needs_cleanup": False},
        "box.jpg": {"category": "original_packaging", "needs_cleanup": False},
    }
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    result = adapters.image_process({"submission_id": "sub-4", "images": images}, "PRODKEY")

    assert result["flags"]["ActualProductPhotosAvailable"]["value"] == "Yes"
    assert result["flags"]["OriginalPackagingPhotosAvailable"]["value"] == "Yes"
    assert result["flags"]["ContextualProductPhotosAvailable"]["value"] == "No"


def test_image_process_real_mode_no_actual_product_photo_leaves_hero_null_and_uses_supporting(monkeypatch, tmp_path):
    """When no submitted image classifies as actual_product, `hero` stays NULL()
    -- there is no Tier-2 fallback any more -- and the non-actual-product photo
    still becomes a supporting image rather than being dropped from the result."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["box.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"box.jpg": {"category": "contextual", "needs_cleanup": False}}
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    result = adapters.image_process({"submission_id": "sub-11", "images": images}, "PRODKEY")

    assert result["hero"] == adapters.NULL()
    assert "website_hero" in result
    assert len(result["supporting"]) == 1
    assert any("box.jpg" in s["value"] for s in result["supporting"])


def test_image_process_gates_website_hero_on_own_field_not_actual_hero(monkeypatch, tmp_path):
    """Website-hero generation must be gated on already_has_website_hero, NOT
    already_has_hero -- the actual-item hero and the AI-generated website hero
    are independent fields. A product with no actual-item hero but a confident
    website hero already on file must NOT trigger another (expensive)
    generation+QC cycle."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["unit.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"unit.jpg": {"category": "actual_product", "needs_cleanup": False}}
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=[])

    generate_calls = []
    monkeypatch.setattr(
        adapters, "_generate_website_hero",
        lambda *a, **k: generate_calls.append(1) or (adapters.NULL(), False, "", ""),
    )

    identity = {"Brand": adapters.E("Acme", 0.9, "vision")}
    adapters.image_process(
        {"submission_id": "sub-gate", "images": images}, "PRODKEY",
        identity=identity, already_has_hero=False, already_has_website_hero=True,
    )

    assert generate_calls == []  # already has a website hero -> no generation call


def test_image_process_generates_website_hero_when_not_already_has_website_hero(monkeypatch, tmp_path):
    """The converse of the gate test above: identity present and
    already_has_website_hero=False (default) DOES trigger generation --
    already_has_hero being True must not suppress it."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["unit.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"unit.jpg": {"category": "actual_product", "needs_cleanup": False}}
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=[])

    generate_calls = []
    monkeypatch.setattr(
        adapters, "_generate_website_hero",
        lambda *a, **k: generate_calls.append(1) or (adapters.NULL(), False, "", ""),
    )

    identity = {"Brand": adapters.E("Acme", 0.9, "vision")}
    adapters.image_process(
        {"submission_id": "sub-gate-2", "images": images}, "PRODKEY",
        identity=identity, already_has_hero=True, already_has_website_hero=False,
    )

    assert generate_calls == [1]  # already_has_hero=True must not suppress generation


def test_image_process_real_mode_logs_cost_per_call(monkeypatch, tmp_path):
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    names = ["a.jpg", "b.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "a.jpg": {"category": "actual_product", "needs_cleanup": False},
        "b.jpg": {"category": "actual_product", "needs_cleanup": True,
                  "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    ledger = CostLedger()
    adapters.image_process({"submission_id": "sub-5", "images": images}, "PRODKEY", ledger=ledger)

    services = [r["service"] for r in ledger.rows]
    assert services.count("openai_vision_classify") == 2   # one per submitted image
    assert services.count("photoroom") == 2                # one per chosen image
    assert services.count("clipdrop_cleanup") == 1          # only the flagged image
    assert all(r["submission_id"] == "sub-5" for r in ledger.rows)


def test_image_process_real_mode_no_images_returns_null_hero_and_all_flags_no(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)

    result = adapters.image_process({"submission_id": "sub-6", "images": []}, "PRODKEY")

    assert result["hero"]["value"] is None
    assert result["supporting"] == []
    assert all(f["value"] == "No" for f in result["flags"].values())


def test_image_process_local_mode_writes_real_files_and_returns_file_urls(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    local_dir = tmp_path / "image-store"
    monkeypatch.setattr(adapters, "LOCAL_IMAGE_DIR", str(local_dir))

    src_dir = tmp_path / "uploads"
    src_dir.mkdir()
    names = ["unit1.jpg", "unit2.jpg"]
    images = _make_images(src_dir, names)
    classify_by_name = {name: {"category": "actual_product", "needs_cleanup": False} for name in names}
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)
    # _install_image_process_fakes patches _connect_storage to S3 mode (for the
    # existing S3-mode tests that share this fixture); override it back to local
    # mode here, after the fixture call, so this override is the one in effect.
    monkeypatch.setattr(adapters, "_connect_storage", lambda: {"mode": "local", "client": None})

    result = adapters.image_process({"submission_id": "sub-local-1", "images": images}, "PRODKEY")

    hero_url = result["hero"]["value"]
    assert hero_url.startswith("file://")
    hero_path = Path(hero_url[len("file://"):])
    assert hero_path.is_file()
    assert hero_path.read_bytes() == _png_bytes()

    for env in result["supporting"]:
        supporting_path = Path(env["value"][len("file://"):])
        assert supporting_path.is_file()

    assert result["original_folder"]["value"] == f"file://{(local_dir / 'originals' / 'sub-local-1').resolve()}/"
    assert result["scrubbed_folder"]["value"] == f"file://{(local_dir / 'scrubbed' / 'sub-local-1').resolve()}/"
    for name in names:
        assert (local_dir / "originals" / "sub-local-1" / name).is_file()
        assert (local_dir / "scrubbed" / "sub-local-1" / name).is_file()


def test_image_process_local_mode_gemini_provider_still_calls_gemini_functions(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "gemini")
    local_dir = tmp_path / "image-store"
    monkeypatch.setattr(adapters, "LOCAL_IMAGE_DIR", str(local_dir))

    src_dir = tmp_path / "uploads"
    src_dir.mkdir()
    names = ["clean.jpg"]
    images = _make_images(src_dir, names)
    classify_by_name = {"clean.jpg": {"category": "actual_product", "needs_cleanup": False}}
    bg_calls, inpaint_calls = [], []
    _install_gemini_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                         bg_calls=bg_calls, inpaint_calls=inpaint_calls)
    # _install_gemini_image_process_fakes patches _connect_storage to S3 mode
    # (for the existing S3-mode tests that share this fixture); override it back
    # to local mode here, after the fixture call, so this override is the one in effect.
    monkeypatch.setattr(adapters, "_connect_storage", lambda: {"mode": "local", "client": None})

    result = adapters.image_process({"submission_id": "sub-local-2", "images": images}, "PRODKEY")

    assert len(bg_calls) == 1
    assert result["hero"]["value"].startswith("file://")
    assert (local_dir / "finished" / "PRODKEY" / "clean.jpg").is_file()


def _install_gemini_image_process_fakes(monkeypatch, *, classify_by_name, bg_calls, inpaint_calls):
    from resale_listing_ai import adapters

    def fake_classify(path, *, ledger, submission_id, config=None):
        if ledger is not None:
            ledger.add(submission_id, "images", "openai_vision_classify", 0.01)
        return adapters._map_classify_output(classify_by_name[Path(path).name])

    def fake_gemini_bg(image_bytes, *, ledger, submission_id):
        bg_calls.append(image_bytes)
        if ledger is not None:
            ledger.add(submission_id, "images", "gemini_bg_remove", 0.03)
        return _png_bytes()

    def fake_gemini_inpaint(marked_bytes, *, ledger, submission_id):
        inpaint_calls.append(marked_bytes)
        if ledger is not None:
            ledger.add(submission_id, "images", "gemini_inpaint", 0.04)
        return b"cleaned:" + marked_bytes

    def fake_s3_put(client, key, data, content_type="image/jpeg"):
        return f"s3://test-bucket/{key}"

    monkeypatch.setattr(adapters, "_classify_image", fake_classify)
    monkeypatch.setattr(adapters, "_gemini_remove_background", fake_gemini_bg)
    monkeypatch.setattr(adapters, "_gemini_inpaint_cleanup", fake_gemini_inpaint)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: object())
    monkeypatch.setattr(adapters, "_s3_put", fake_s3_put)
    monkeypatch.setattr(adapters, "_connect_storage", lambda: {"mode": "s3", "client": object()})


def _install_bedrock_image_process_fakes(monkeypatch, *, classify_by_name, bg_calls, inpaint_calls):
    from resale_listing_ai import adapters

    def fake_classify(path, *, ledger, submission_id, config=None):
        if ledger is not None:
            ledger.add(submission_id, "images", "openai_vision_classify", 0.01)
        return adapters._map_classify_output(classify_by_name[Path(path).name])

    def fake_bedrock_bg(image_bytes, *, ledger, submission_id):
        bg_calls.append(image_bytes)
        if ledger is not None:
            ledger.add(submission_id, "images", "bedrock_bg_remove", 0.02)
        return _png_bytes()

    def fake_bedrock_inpaint(image_bytes, mask_bytes, *, ledger, submission_id):
        inpaint_calls.append((image_bytes, mask_bytes))
        if ledger is not None:
            ledger.add(submission_id, "images", "bedrock_inpaint", 0.02)
        return b"cleaned:" + image_bytes

    def fake_s3_put(client, key, data, content_type="image/jpeg"):
        return f"s3://test-bucket/{key}"

    monkeypatch.setattr(adapters, "_classify_image", fake_classify)
    monkeypatch.setattr(adapters, "_bedrock_remove_background", fake_bedrock_bg)
    monkeypatch.setattr(adapters, "_bedrock_inpaint_cleanup", fake_bedrock_inpaint)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: object())
    monkeypatch.setattr(adapters, "_s3_put", fake_s3_put)
    monkeypatch.setattr(adapters, "_connect_storage", lambda: {"mode": "s3", "client": object()})


def test_image_process_bedrock_provider_only_cleans_flagged_images(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "bedrock")
    names = ["clean.jpg", "has_tape.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "clean.jpg": {"category": "actual_product", "needs_cleanup": False},
        "has_tape.jpg": {"category": "actual_product", "needs_cleanup": True,
                          "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    bg_calls, inpaint_calls = [], []
    _install_bedrock_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                          bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    result = adapters.image_process({"submission_id": "sub-b1", "images": images}, "PRODKEY")

    assert len(bg_calls) == 2       # background removal always runs, for every chosen image
    assert len(inpaint_calls) == 1  # inpaint only for the flagged image
    # The bedrock path passes an explicit MASK, not a drawn-marker image — the
    # image_bytes arg must be the plain scrubbed bytes (unlike gemini's marked copy).
    inpaint_image_bytes, inpaint_mask_bytes = inpaint_calls[0]
    assert inpaint_image_bytes == _png_bytes()
    assert inpaint_mask_bytes is not None
    finished_urls = [result["hero"]["value"]] + [s["value"] for s in result["supporting"]]
    assert any("has_tape.jpg" in u for u in finished_urls)
    assert any("clean.jpg" in u for u in finished_urls)


def test_image_process_bedrock_provider_flagged_without_bbox_skips_inpaint(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "bedrock")
    names = ["mystery_clutter.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"mystery_clutter.jpg": {"category": "actual_product", "needs_cleanup": True,
                                                 "clutter_bbox": None}}
    bg_calls, inpaint_calls = [], []
    _install_bedrock_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                          bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    adapters.image_process({"submission_id": "sub-b2", "images": images}, "PRODKEY")

    assert inpaint_calls == []


def test_image_process_bedrock_provider_logs_cost_per_call(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "bedrock")
    names = ["a.jpg", "b.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "a.jpg": {"category": "actual_product", "needs_cleanup": False},
        "b.jpg": {"category": "actual_product", "needs_cleanup": True,
                  "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    bg_calls, inpaint_calls = [], []
    _install_bedrock_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                          bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    ledger = CostLedger()
    adapters.image_process({"submission_id": "sub-b3", "images": images}, "PRODKEY", ledger=ledger)

    services = [r["service"] for r in ledger.rows]
    assert services.count("bedrock_bg_remove") == 2
    assert services.count("bedrock_inpaint") == 1
    assert all(r["submission_id"] == "sub-b3" for r in ledger.rows)


def test_image_process_bedrock_provider_never_calls_bbox_marker(monkeypatch, tmp_path):
    """The bedrock path uses an explicit mask (_build_cleanup_mask) — it must
    never call _draw_bbox_marker, which is gemini-only."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "bedrock")
    names = ["has_tape.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"has_tape.jpg": {"category": "actual_product", "needs_cleanup": True,
                                          "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}}
    bg_calls, inpaint_calls = [], []
    _install_bedrock_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                          bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    marker_calls = []
    monkeypatch.setattr(adapters, "_draw_bbox_marker",
                         lambda *a, **k: marker_calls.append(1) or None)

    adapters.image_process({"submission_id": "sub-b4", "images": images}, "PRODKEY")

    assert marker_calls == []


def test_image_process_gemini_provider_only_cleans_flagged_images(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "gemini")
    names = ["clean.jpg", "has_tape.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "clean.jpg": {"category": "actual_product", "needs_cleanup": False},
        "has_tape.jpg": {"category": "actual_product", "needs_cleanup": True,
                          "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    bg_calls, inpaint_calls = [], []
    _install_gemini_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                         bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    result = adapters.image_process({"submission_id": "sub-g1", "images": images}, "PRODKEY")

    assert len(bg_calls) == 2       # background removal always runs, for every chosen image
    assert len(inpaint_calls) == 1  # inpaint only for the flagged image
    # The bytes reaching the inpaint call must be the MARKED bytes (scrubbed image
    # plus the drawn bbox rectangle), not the plain scrubbed bytes _gemini_remove_background
    # returned -- a swapped variable in the branch (passing scrubbed_bytes straight through)
    # would still pass every assertion above.
    assert inpaint_calls[0] != _png_bytes()
    finished_urls = [result["hero"]["value"]] + [s["value"] for s in result["supporting"]]
    assert any("has_tape.jpg" in u for u in finished_urls)
    assert any("clean.jpg" in u for u in finished_urls)


def test_image_process_gemini_provider_flagged_without_bbox_skips_inpaint(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "gemini")
    names = ["mystery_clutter.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"mystery_clutter.jpg": {"category": "actual_product", "needs_cleanup": True,
                                                 "clutter_bbox": None}}
    bg_calls, inpaint_calls = [], []
    _install_gemini_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                         bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    adapters.image_process({"submission_id": "sub-g2", "images": images}, "PRODKEY")

    assert inpaint_calls == []


def test_image_process_gemini_provider_logs_cost_per_call(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "gemini")
    names = ["a.jpg", "b.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {
        "a.jpg": {"category": "actual_product", "needs_cleanup": False},
        "b.jpg": {"category": "actual_product", "needs_cleanup": True,
                  "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    }
    bg_calls, inpaint_calls = [], []
    _install_gemini_image_process_fakes(monkeypatch, classify_by_name=classify_by_name,
                                         bg_calls=bg_calls, inpaint_calls=inpaint_calls)

    ledger = CostLedger()
    adapters.image_process({"submission_id": "sub-g3", "images": images}, "PRODKEY", ledger=ledger)

    services = [r["service"] for r in ledger.rows]
    assert services.count("gemini_bg_remove") == 2
    assert services.count("gemini_inpaint") == 1
    assert all(r["submission_id"] == "sub-g3" for r in ledger.rows)


def test_image_process_photoroom_provider_never_calls_bbox_marker(monkeypatch, tmp_path):
    """Default provider path must never draw a marker — that's gemini-only."""
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", raising=False)
    names = ["has_tape.jpg"]
    images = _make_images(tmp_path, names)
    classify_by_name = {"has_tape.jpg": {"category": "actual_product", "needs_cleanup": True,
                                          "clutter_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}}
    cleanup_calls = []
    _install_image_process_fakes(monkeypatch, classify_by_name=classify_by_name, cleanup_calls=cleanup_calls)

    marker_calls = []
    monkeypatch.setattr(adapters, "_draw_bbox_marker",
                         lambda *a, **k: marker_calls.append(1) or None)

    adapters.image_process({"submission_id": "sub-g4", "images": images}, "PRODKEY")

    assert marker_calls == []


# ---------------------------------------------------------------------------
# generate_copy — copy stage, constrained to VERIFIED (confident) Product facts
# ---------------------------------------------------------------------------

def test_copy_prompt_config_matches_the_loaded_constants():
    from resale_listing_ai import adapters

    assert adapters.COPY_FIELDS == [
        "MasterTitle", "ShortTitle", "MasterDescription",
        "BulletPoint1", "BulletPoint2", "BulletPoint3", "BulletPoint4", "BulletPoint5",
        "SearchTerms", "SEOKeywords",
    ]
    assert "never invent" in adapters.COPY_INSTRUCTIONS
    assert adapters.COPY_JSON_SCHEMA["required"] == adapters.COPY_FIELDS
    assert adapters.COPY_JSON_SCHEMA["properties"]["MasterTitle"] == {"type": ["string", "null"]}


def _product_with(**overrides):
    from resale_listing_ai.records import blank_product

    product = blank_product()
    for field, env in overrides.items():
        product[field] = env
    return product


def test_generate_copy_sends_only_confident_fields_to_the_model(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
        Colour={"value": "Ash", "confidence": 0.8, "source": "vision"},
        Material={"value": "Unobtainium", "confidence": 0.2, "source": "vision"},  # below the 0.60 gate
    )
    raw = {f: None for f in adapters.COPY_FIELDS}
    raw["MasterTitle"] = "Acme Smart Doorbell Ash"
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    adapters.generate_copy(product)

    sent = fake_client.responses.last_call["input"][0]["content"]
    sent_text = " ".join(part["text"] for part in sent)
    assert "Acme" in sent_text
    assert "Smart Doorbell" in sent_text
    assert "Unobtainium" not in sent_text  # below the confidence gate -> never shown to the model


def test_generate_copy_omits_fields_the_model_returns_null_for(monkeypatch):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )
    raw = {f: None for f in adapters.COPY_FIELDS}
    raw["MasterTitle"] = "Acme Smart Doorbell"
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    result = adapters.generate_copy(product)

    assert set(result) == {"MasterTitle"}
    assert result["MasterTitle"]["value"] == "Acme Smart Doorbell"
    assert result["MasterTitle"]["source"] == "generated"


def test_generate_copy_logs_cost_to_the_ledger(monkeypatch):
    import json

    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )
    raw = {f: None for f in adapters.COPY_FIELDS}
    raw["MasterTitle"] = "Acme Smart Doorbell"
    fake_client = _FakeOpenAIClient(json.dumps(raw))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    ledger = CostLedger()
    adapters.generate_copy(product, ledger=ledger, submission_id="sub-9")

    assert len(ledger.rows) == 1
    assert ledger.rows[0]["stage"] == "copy"
    assert ledger.rows[0]["submission_id"] == "sub-9"


def test_generate_copy_no_confident_brand_or_name_short_circuits_without_calling_api(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    product = _product_with(Brand={"value": "Acme", "confidence": 0.9, "source": "vision"})  # no ProductName

    def _boom():
        raise AssertionError("must not call the API with nothing solid enough to write copy from")

    monkeypatch.setattr(adapters, "_get_openai_client", _boom)

    result = adapters.generate_copy(product)

    assert result == {}


def test_generate_copy_stub_mode_matches_local_concatenation_and_logs_flat_cost(monkeypatch):
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
        Colour={"value": "Ash", "confidence": 0.8, "source": "vision"},
    )
    ledger = CostLedger()

    result = adapters.generate_copy(product, ledger=ledger, submission_id="sub-1")

    assert result["MasterTitle"]["value"] == "Acme Smart Doorbell Ash"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "llm"


# ---------------------------------------------------------------------------
# Vision/copy provider dispatch — Gemini and OpenRouter (alongside OpenAI)
# ---------------------------------------------------------------------------

def test_vision_provider_defaults_to_openai(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)

    assert adapters._vision_provider() == "openai"


def test_vision_provider_uses_admin_override_over_env_and_file_default(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)
    assert adapters._vision_provider() == "openai"  # file default
    assert adapters._vision_provider(config={"provider_stack.vision_provider": "gemini"}) == "gemini"

    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    assert adapters._vision_provider() == "openrouter"  # env beats file default
    assert adapters._vision_provider(config={"provider_stack.vision_provider": "bedrock"}) == "bedrock"  # override beats env


def test_copy_provider_defaults_to_openai(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_COPY_PROVIDER", raising=False)

    assert adapters._copy_provider() == "openai"


def test_copy_provider_uses_admin_override(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_COPY_PROVIDER", raising=False)
    assert adapters._copy_provider(config={"provider_stack.copy_provider": "gemini"}) == "gemini"


def test_vision_identify_dispatches_to_gemini_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    calls = {}

    def fake_vision_json(instructions, notes, images):
        calls["notes"] = notes
        return json.dumps(RAW_VISION_OUTPUT)

    monkeypatch.setattr(gemini, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    identity = adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": "note"})

    assert calls["notes"] == "note"
    assert identity["Brand"]["value"] == "Acme"


def test_vision_identify_dispatches_to_openrouter_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    monkeypatch.setattr(
        openrouter, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_VISION_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    identity = adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})

    assert identity["Brand"]["value"] == "Acme"


def test_vision_identify_gemini_requires_api_key_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    with pytest.raises(RuntimeError):
        adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})


def test_vision_identify_dispatches_to_bedrock_when_selected(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    calls = {}

    def fake_vision_json(instructions, notes, images):
        calls["notes"] = notes
        return json.dumps(RAW_VISION_OUTPUT)

    monkeypatch.setattr(bedrock, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    identity = adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": "note"})

    assert calls["notes"] == "note"
    assert identity["Brand"]["value"] == "Acme"


def test_vision_identify_bedrock_requires_aws_credentials_when_selected(monkeypatch, tmp_path):
    import boto3

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    class _FakeSession:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", _FakeSession)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    with pytest.raises(RuntimeError):
        adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})


def test_image_classify_prompt_config_matches_the_loaded_constants():
    """Asserts literal expected values, not a reload-and-compare against the
    same _load_prompt() call that already produced these constants -- image_classify
    has no field-list assembly step to verify (unlike vision_identify/unit_observe/
    copy), so a reload comparison here would be 100% tautological. Real content
    verification: a mid-execution review caught this pattern as too weak; see
    ledger for the Task 2-4 fix-round history."""
    from resale_listing_ai import adapters

    assert adapters.IMAGE_CATEGORIES == (
        "actual_product", "original_packaging", "product_information_label", "contextual")
    assert "never around the product itself" in adapters.IMAGE_CLASSIFY_INSTRUCTIONS
    assert adapters._IMAGE_CLASSIFY_SCHEMA["required"] == ["category", "needs_cleanup", "clutter_bbox"]


def test_classify_image_dispatches_to_gemini_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(
        gemini, "classify_json",
        lambda instructions, image:
            '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}')

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._classify_image(str(img), ledger=None, submission_id="x")

    assert result["category"] == "actual_product"


def test_classify_image_logs_gemini_service_to_ledger(monkeypatch, tmp_path):
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(
        gemini, "classify_json",
        lambda instructions, image:
            '{"category": "contextual", "needs_cleanup": false, "clutter_bbox": null}')

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters._classify_image(str(img), ledger=ledger, submission_id="sub-1")

    assert ledger.rows[0]["service"] == "gemini_vision_classify"
    assert ledger.rows[0]["stage"] == "images"


def test_classify_image_dispatches_to_bedrock_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    monkeypatch.setattr(
        bedrock, "classify_json",
        lambda instructions, image:
            '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}')

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._classify_image(str(img), ledger=None, submission_id="x")

    assert result["category"] == "actual_product"


def test_classify_image_logs_bedrock_service_to_ledger(monkeypatch, tmp_path):
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    monkeypatch.setattr(
        bedrock, "classify_json",
        lambda instructions, image:
            '{"category": "contextual", "needs_cleanup": false, "clutter_bbox": null}')

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters._classify_image(str(img), ledger=ledger, submission_id="sub-1")

    assert ledger.rows[0]["service"] == "bedrock_vision_classify"
    assert ledger.rows[0]["stage"] == "images"


def test_hero_qc_real_mode_returns_mapped_dict_when_all_checks_pass(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps({
        "branding_clear": True, "coherent": True,
        "matches_confirmed": True, "no_invented": True, "reasons": []}))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=None, submission_id="x")

    assert result == {"branding_clear": True, "coherent": True,
                       "matches_confirmed": True, "no_invented": True, "reasons": []}
    assert fake_client.responses.last_call is not None


def test_hero_qc_returns_mapped_dict_with_reasons_when_a_check_fails(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps({
        "branding_clear": False, "coherent": True,
        "matches_confirmed": True, "no_invented": True,
        "reasons": ["logo visible on the front panel"]}))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=None, submission_id="x")

    assert result["branding_clear"] is False
    assert result["reasons"] == ["logo visible on the front panel"]


def test_hero_qc_dispatches_to_gemini_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    seen = {}

    def fake_classify_json(instructions, image):
        seen["instructions"] = instructions
        return ('{"branding_clear": true, "coherent": true, '
                '"matches_confirmed": true, "no_invented": true, "reasons": []}')

    monkeypatch.setattr(gemini, "classify_json", fake_classify_json)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=None, submission_id="x")

    assert result["coherent"] is True
    assert "Acme Smart Doorbell" in seen["instructions"]


def test_hero_qc_dispatches_to_bedrock_when_selected(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")
    seen = {}

    def fake_classify_json(instructions, image):
        seen["instructions"] = instructions
        return ('{"branding_clear": true, "coherent": true, '
                '"matches_confirmed": true, "no_invented": true, "reasons": []}')

    monkeypatch.setattr(bedrock, "classify_json", fake_classify_json)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    result = adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=None, submission_id="x")

    assert result["coherent"] is True
    assert "Acme Smart Doorbell" in seen["instructions"]


def test_hero_qc_returns_none_on_malformed_model_output(monkeypatch, tmp_path):
    """Malformed model output must degrade to None (a failed check), never a
    silent pass -- an unparseable/invalid result must never be assumed to be
    success."""
    import json

    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps({"unexpected": "shape"}))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)
    monkeypatch.setattr(adapters, "repair_json", lambda *a, **k: None)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())

    assert adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=None, submission_id="x") is None


def test_hero_qc_logs_cost_to_ledger(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)
    fake_client = _FakeOpenAIClient(json.dumps({
        "branding_clear": True, "coherent": True,
        "matches_confirmed": True, "no_invented": True, "reasons": []}))
    monkeypatch.setattr(adapters, "_get_openai_client", lambda: fake_client)

    img = tmp_path / "generated.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters._hero_qc(str(img), "Acme Smart Doorbell", ledger=ledger, submission_id="sub-1")

    assert ledger.rows[0]["stage"] == "images"
    assert ledger.rows[0]["service"] == "openai_hero_qc"
    assert ledger.rows[0]["cost_cad"] == adapters.HERO_QC_UNIT_COST_CAD


def test_generate_copy_dispatches_to_openrouter_when_selected(monkeypatch):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_COPY_PROVIDER", "openrouter")
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )
    raw = {f: None for f in adapters.COPY_FIELDS}
    raw["MasterTitle"] = "Acme Smart Doorbell"
    monkeypatch.setattr(openrouter, "copy_json", lambda instructions, facts_text: json.dumps(raw))

    result = adapters.generate_copy(product)

    assert result["MasterTitle"]["value"] == "Acme Smart Doorbell"


def test_generate_copy_openrouter_requires_api_key_when_selected(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_COPY_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )

    with pytest.raises(RuntimeError):
        adapters.generate_copy(product)


# ---------------------------------------------------------------------------
# Web-search provider dispatch — Gemini and OpenRouter (alongside OpenAI/Perplexity)
# ---------------------------------------------------------------------------

def test_web_search_provider_gemini_search_option(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    assert adapters._web_search_provider() == "gemini_search"


def test_web_search_provider_openrouter_online_option(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openrouter_online")

    assert adapters._web_search_provider() == "openrouter_online"


def test_image_process_provider_defaults_to_photoroom_clipdrop(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", raising=False)

    assert adapters._image_process_provider() == "photoroom_clipdrop"


def test_image_process_provider_gemini_option(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "gemini")

    assert adapters._image_process_provider() == "gemini"


def test_image_process_provider_bedrock_option(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", "bedrock")

    assert adapters._image_process_provider() == "bedrock"


def test_image_process_provider_uses_admin_override(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", raising=False)
    assert adapters._image_process_provider(
        config={"provider_stack.image_process_provider": "gemini"}) == "gemini"


def test_hero_generate_provider_defaults_to_photoroom(monkeypatch):
    from resale_listing_ai import adapters
    monkeypatch.delenv("RESALE_LISTING_AI_HERO_GENERATE_PROVIDER", raising=False)
    assert adapters._hero_generate_provider() == "photoroom"


def test_generate_hero_bytes_dispatches_to_photoroom_provider(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "photoroom")
    calls = []
    monkeypatch.setattr(
        photoroom_image, "generate_hero_image",
        lambda instructions, reference_bytes=None: calls.append((instructions, reference_bytes)) or b"pr-hero")

    out = adapters._generate_hero_bytes("draw it", b"ref", ledger=None, submission_id="s")

    assert out == b"pr-hero"
    assert calls == [("draw it", b"ref")]


def test_generate_hero_bytes_photoroom_4xx_fails_fast(monkeypatch):
    import requests
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import photoroom_image

    resp = requests.Response()
    resp.status_code = 402  # payment/entitlement -- not retryable
    calls = []

    def _boom(instructions, reference_bytes=None):
        calls.append(instructions)
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "photoroom")
    monkeypatch.setattr(photoroom_image, "generate_hero_image", _boom)

    with pytest.raises(requests.HTTPError):
        adapters._generate_hero_bytes("draw it", None, ledger=None, submission_id="s")

    assert len(calls) == 1  # 4xx entitlement error is not retried


def test_generate_hero_bytes_photoroom_429_retries(monkeypatch):
    import requests
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import photoroom_image

    resp = requests.Response()
    resp.status_code = 429  # rate limit -- transient
    calls = []

    def _boom(instructions, reference_bytes=None):
        calls.append(instructions)
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "photoroom")
    monkeypatch.setattr(photoroom_image, "generate_hero_image", _boom)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)

    with pytest.raises(requests.HTTPError):
        adapters._generate_hero_bytes("draw it", None, ledger=None, submission_id="s")

    assert len(calls) == 3  # 429 retried max_retries (2) + initial = 3


def test_hero_generate_provider_env_override_wins(monkeypatch):
    from resale_listing_ai import adapters
    monkeypatch.setenv("RESALE_LISTING_AI_HERO_GENERATE_PROVIDER", "gemini")
    assert adapters._hero_generate_provider() == "gemini"


def test_generate_hero_bytes_dispatches_to_openai_provider(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openai_image

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "openai")
    calls = []
    monkeypatch.setattr(
        openai_image, "generate_hero_image",
        lambda instructions, reference_bytes=None: calls.append((instructions, reference_bytes)) or b"openai-hero")

    out = adapters._generate_hero_bytes("draw it", None, ledger=None, submission_id="s")

    assert out == b"openai-hero"
    assert calls == [("draw it", None)]


def test_generate_hero_bytes_openai_permission_error_fails_fast(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openai_image

    class _FakePermError(Exception):
        status_code = 401

    calls = []

    def _boom(instructions, reference_bytes=None):
        calls.append(instructions)
        raise _FakePermError("insufficient permissions")

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "openai")
    monkeypatch.setattr(openai_image, "generate_hero_image", _boom)

    with pytest.raises(_FakePermError):
        adapters._generate_hero_bytes("draw it", None, ledger=None, submission_id="s")

    assert len(calls) == 1  # 401 is not retried -- one attempt, then fail fast


def test_generate_hero_bytes_openai_transient_error_retries(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openai_image

    calls = []

    def _boom(instructions, reference_bytes=None):
        calls.append(instructions)
        raise adapters._APIConnectionError(request=None)

    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "openai")
    monkeypatch.setattr(openai_image, "generate_hero_image", _boom)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)  # no real backoff delay

    with pytest.raises(adapters._APIConnectionError):
        adapters._generate_hero_bytes("draw it", None, ledger=None, submission_id="s")

    assert len(calls) == 3  # transient -> retried max_retries (2) + initial = 3 attempts


def test_hero_generate_provider_bedrock_option(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_HERO_GENERATE_PROVIDER", "bedrock")

    assert adapters._hero_generate_provider() == "bedrock"


def test_hero_generate_provider_uses_admin_override(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_HERO_GENERATE_PROVIDER", raising=False)
    assert adapters._hero_generate_provider(
        config={"provider_stack.hero_generate_provider": "bedrock"}) == "bedrock"


def test_bedrock_retry_errors_includes_boto_client_error():
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters

    assert ClientError in adapters._BEDROCK_RETRY_ERRORS


def test_gemini_remove_background_logs_cost_and_returns_bytes(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.setattr(gemini, "remove_background_image", lambda image_bytes: b"scrubbed")
    ledger = CostLedger()

    result = adapters._gemini_remove_background(b"original", ledger=ledger, submission_id="sub-1")

    assert result == b"scrubbed"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "gemini_bg_remove"
    assert ledger.rows[0]["stage"] == "images"


def test_gemini_inpaint_cleanup_logs_cost_and_returns_bytes(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.setattr(gemini, "inpaint_region_image", lambda marked_bytes: b"cleaned")
    ledger = CostLedger()

    result = adapters._gemini_inpaint_cleanup(b"marked", ledger=ledger, submission_id="sub-1")

    assert result == b"cleaned"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "gemini_inpaint"
    assert ledger.rows[0]["stage"] == "images"


def _gemini_image_rate_limit_error():
    from google.genai import errors

    return errors.ClientError(429, {"error": {"message": "rate limit", "status": "RESOURCE_EXHAUSTED"}})


def test_gemini_remove_background_rate_limit_is_retried_and_logged(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    def always_rate_limited(image_bytes):
        raise _gemini_image_rate_limit_error()

    monkeypatch.setattr(gemini, "remove_background_image", always_rate_limited)
    ledger = CostLedger()

    with pytest.raises(Exception):
        adapters._gemini_remove_background(b"original", ledger=ledger, submission_id="sub-429")

    assert len(ledger.rows) == 3  # 1 initial attempt + 2 retries, each logged
    assert all(r["service"] == "gemini_bg_remove" for r in ledger.rows)
    assert [r["retry"] for r in ledger.rows] == [False, True, True]


def test_bedrock_remove_background_logs_cost_and_returns_bytes(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    monkeypatch.setattr(bedrock, "remove_background_image", lambda image_bytes: b"scrubbed")
    ledger = CostLedger()

    result = adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-1")

    assert result == b"scrubbed"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "bedrock_bg_remove"
    assert ledger.rows[0]["stage"] == "images"


def test_bedrock_inpaint_cleanup_logs_cost_and_returns_bytes(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    monkeypatch.setattr(bedrock, "inpaint_region_image", lambda image_bytes, mask_bytes: b"cleaned")
    ledger = CostLedger()

    result = adapters._bedrock_inpaint_cleanup(b"scrubbed", b"mask", ledger=ledger, submission_id="sub-1")

    assert result == b"cleaned"
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "bedrock_inpaint"
    assert ledger.rows[0]["stage"] == "images"


def _bedrock_client_error():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "rate limited"}}, "InvokeModel")


def test_bedrock_remove_background_throttle_is_retried_and_logged(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_throttled(image_bytes):
        raise _bedrock_client_error()

    monkeypatch.setattr(bedrock, "remove_background_image", always_throttled)
    ledger = CostLedger()

    with pytest.raises(Exception):
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-429")

    assert len(ledger.rows) == 3  # 1 initial attempt + 2 retries, each logged
    assert all(r["service"] == "bedrock_bg_remove" for r in ledger.rows)
    assert [r["retry"] for r in ledger.rows] == [False, True, True]


def _bedrock_validation_error():
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "image exceeds pixel limit"}},
        "InvokeModel")


def test_bedrock_remove_background_validation_error_is_not_retried(monkeypatch):
    """A ValidationException (bad model id, oversized image, alpha channel, mask
    mismatch) can never succeed on retry — retrying it bills three times for
    work that cannot work. One attempt, one cost row, original error raised."""
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_invalid(image_bytes):
        raise _bedrock_validation_error()

    monkeypatch.setattr(bedrock, "remove_background_image", always_invalid)
    ledger = CostLedger()

    with pytest.raises(ClientError) as excinfo:
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-bad")

    assert excinfo.value.response["Error"]["Code"] == "ValidationException"
    assert len(ledger.rows) == 1  # NOT 3 — no retries
    assert ledger.rows[0]["retry"] is False
    assert ledger.rows[0]["service"] == "bedrock_bg_remove"


def test_bedrock_inpaint_validation_error_is_not_retried(monkeypatch):
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_invalid(image_bytes, mask_bytes):
        raise _bedrock_validation_error()

    monkeypatch.setattr(bedrock, "inpaint_region_image", always_invalid)
    ledger = CostLedger()

    with pytest.raises(ClientError):
        adapters._bedrock_inpaint_cleanup(b"img", b"mask", ledger=ledger, submission_id="sub-bad")

    assert len(ledger.rows) == 1


def test_bedrock_access_denied_is_not_retried(monkeypatch):
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_denied(image_bytes):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "InvokeModel")

    monkeypatch.setattr(bedrock, "remove_background_image", always_denied)
    ledger = CostLedger()

    with pytest.raises(ClientError):
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-403")

    assert len(ledger.rows) == 1


@pytest.mark.parametrize("code", [
    "ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException",
    "TooManyRequestsException", "InternalServerException",
])
def test_bedrock_transient_codes_are_still_retried(monkeypatch, code):
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_transient(image_bytes):
        raise ClientError({"Error": {"Code": code, "Message": "try again"}}, "InvokeModel")

    monkeypatch.setattr(bedrock, "remove_background_image", always_transient)
    ledger = CostLedger()

    with pytest.raises(ClientError):
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-retry")

    assert len(ledger.rows) == 3


def test_bedrock_read_timeout_is_retried_and_logged(monkeypatch):
    """A ReadTimeoutError is a BotoCoreError, NOT a ClientError — but the
    transport failing is exactly the case a retry can fix, and every attempt was
    still a real (billable) call, so all three attempts must reach the ledger."""
    from botocore.exceptions import ReadTimeoutError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_timeout(image_bytes):
        raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

    monkeypatch.setattr(bedrock, "remove_background_image", always_timeout)
    ledger = CostLedger()

    with pytest.raises(ReadTimeoutError):
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-timeout")

    assert len(ledger.rows) == 3  # 1 initial attempt + 2 retries, each logged
    assert all(r["service"] == "bedrock_bg_remove" for r in ledger.rows)
    assert [r["retry"] for r in ledger.rows] == [False, True, True]


def test_bedrock_credential_error_is_logged_once_and_not_retried(monkeypatch):
    """NoCredentialsError is a BotoCoreError with no ClientError response code.
    Retrying it bills three times for work that can never succeed, but it must
    still be logged once — an unlogged failure hides a real call (project rule)."""
    from botocore.exceptions import NoCredentialsError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_unauthenticated(image_bytes):
        raise NoCredentialsError()

    monkeypatch.setattr(bedrock, "remove_background_image", always_unauthenticated)
    ledger = CostLedger()

    with pytest.raises(NoCredentialsError):
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-noauth")

    assert len(ledger.rows) == 1  # NOT 3 — no retries
    assert ledger.rows[0]["retry"] is False
    assert ledger.rows[0]["service"] == "bedrock_bg_remove"


def test_bedrock_malformed_response_is_logged_once_and_not_retried(monkeypatch):
    """bedrock.py's own response parsing raises ValueError when Nova Canvas
    returns no image. The call was made and billed -> one cost row; the response
    already came back in that shape -> no retry, and the ORIGINAL ValueError
    reaches the caller (not the _NonRetryableProviderError marker)."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    def always_empty(image_bytes):
        raise ValueError("Bedrock Nova Canvas response contained no image")

    monkeypatch.setattr(bedrock, "remove_background_image", always_empty)
    ledger = CostLedger()

    with pytest.raises(ValueError) as excinfo:
        adapters._bedrock_remove_background(b"original", ledger=ledger, submission_id="sub-empty")

    assert "contained no image" in str(excinfo.value)
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["retry"] is False


def test_bedrock_vision_branch_does_not_retry_a_validation_error(monkeypatch, tmp_path):
    """The retry filter covers the Converse/vision dispatch sites too, not just
    the two image ones."""
    from botocore.exceptions import ClientError

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "bedrock")

    def always_invalid(instructions, notes, images):
        raise _bedrock_validation_error()

    monkeypatch.setattr(bedrock, "vision_json", always_invalid)
    ledger = CostLedger()
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"not-a-real-jpeg")

    with pytest.raises(ClientError):
        adapters.vision_identify({"submission_id": "sub-bad", "notes": None, "images": [str(image)]},
                                  ledger=ledger)

    assert len(ledger.rows) == 1


def test_retail_price_dispatches_to_gemini_search_when_selected(monkeypatch):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")
    raw = {
        "match_found": True,
        "anchor": {"price_cad": 149.99, "confidence": 0.74, "source_name": "The Home Depot Canada",
                    "source_url": "https://www.homedepot.ca/product/123", "original_currency": "CAD"},
        "corroborating": None,
        "confidence_label": "Medium",
    }
    monkeypatch.setattr(gemini, "web_search_json", lambda instructions: json.dumps(raw))

    result = adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result["PricingAnchorPriceCAD"]["value"] == 149.99


def test_resale_price_dispatches_to_openrouter_online_when_selected(monkeypatch):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openrouter_online")
    raw = {"match_found": True, "comparable": {
        "brand": "ecobee", "product_name": "ecobee Smart Doorbell Camera (wired)",
        "price_cad": 95.00, "confidence": 0.55, "source_url": "https://www.ebay.ca/itm/123"}}
    monkeypatch.setattr(openrouter, "web_search_json", lambda instructions: json.dumps(raw))

    result = adapters.resale_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    assert result["ComparableProductPriceCAD"]["value"] == 95.00


def test_gemini_search_requires_api_key_when_selected(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)


def test_retail_price_logs_gemini_search_service_to_ledger(monkeypatch):
    import json

    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")
    raw = {"match_found": False, "anchor": None, "corroborating": None, "confidence_label": "Low"}
    monkeypatch.setattr(gemini, "web_search_json", lambda instructions: json.dumps(raw))

    ledger = CostLedger()
    adapters.retail_price({"submission_id": "sub-9", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert ledger.rows[0]["service"] == "gemini_search"
    assert ledger.rows[0]["stage"] == "pricing"


def test_pricing_lookup_no_longer_exists():
    """pricing_lookup was the known gap (identity=None, retail_price never
    called) that test_stage_ingest_calls_retail_and_resale_price_with_real_identity
    in test_pipeline.py now covers via direct retail_price/resale_price calls."""
    from resale_listing_ai import adapters

    assert not hasattr(adapters, "pricing_lookup")


def test_stub_retail_price_logs_the_full_flat_pricing_cost_when_ledger_given():
    """_stub_retail_price alone owns the flat pricing-stage placeholder log
    (matching the old single $0.040 row exactly -- see Global Constraints:
    stub-mode output must stay byte-identical). _stub_resale_price logs
    nothing itself; see test_stub_resale_price_has_no_ledger_parameter."""
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    adapters._stub_retail_price({"submission_id": "x", "hint": "nest-doorbell"}, ledger=ledger)

    assert ledger.rows == [{
        "submission_id": "x", "stage": "pricing", "service": "web_search",
        "cost_cad": 0.040, "retry": False,
        "input_tokens": None, "output_tokens": None, "model": None,
    }]


def test_stub_resale_price_has_no_ledger_parameter():
    """Confirms _stub_resale_price's signature is untouched by this task --
    only _stub_retail_price gained a ledger param, so stub-mode's cost_ledger
    stays exactly one row per pricing lookup, byte-identical to before this
    fix (see test_stub_retail_price_logs_the_full_flat_pricing_cost_when_ledger_given)."""
    import inspect

    from resale_listing_ai import adapters

    params = inspect.signature(adapters._stub_resale_price).parameters
    assert "ledger" not in params


def test_stub_retail_and_resale_price_together_total_the_old_flat_cost():
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import adapters

    ledger = CostLedger()
    adapters._stub_retail_price({"submission_id": "x", "hint": "nest-doorbell"}, ledger=ledger)
    adapters._stub_resale_price({"submission_id": "x", "hint": "nest-doorbell"}, None)

    assert ledger.total("x") == 0.040
    assert len(ledger.rows) == 1  # exactly one row, matching pre-fix stub-mode output


# ---------------------------------------------------------------------------
# Gemini/OpenRouter must be TOLD the target JSON schema in the prompt text
#
# The OpenAI/Perplexity paths convey the target field shape STRUCTURALLY, via
# the API's own json_schema response format. Gemini and OpenRouter are called
# with a loose JSON response mode instead, so the shape has to travel in the
# instructions string -- and json_repair._coerce_to_schema projects onto our
# schema by EXACT key lookup. Drop the schema from the prompt and a real run
# returns keys we never look up, every field nulls out, the gate rejects the
# submission, and both providers' live-demo reports come back identically
# empty. These tests pin the schema into the prompt so that can't regress.
# ---------------------------------------------------------------------------

def test_vision_identify_gemini_prompt_names_the_real_schema_fields(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    seen = {}

    def fake_vision_json(instructions, notes, images):
        seen["instructions"] = instructions
        return json.dumps(RAW_VISION_OUTPUT)

    monkeypatch.setattr(gemini, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})

    instructions = seen["instructions"]
    # the human-readable guidance must survive alongside the schema
    assert "never guess" in instructions
    # real field names the model would otherwise have to invent
    for field in ("Brand", "ProductName", "ModelNumber", "InternalSubcategory"):
        assert field in instructions
    # ...and the {value, confidence, source} envelope shape
    for key in ("value", "confidence", "source"):
        assert key in instructions
    # every required field of the schema, not just a hand-picked few
    for field in adapters.VISION_JSON_SCHEMA["required"]:
        assert field in instructions


def test_vision_identify_openrouter_prompt_names_the_real_schema_fields(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    seen = {}

    def fake_vision_json(instructions, notes, images):
        seen["instructions"] = instructions
        return json.dumps(RAW_VISION_OUTPUT)

    monkeypatch.setattr(openrouter, "vision_json", fake_vision_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    adapters.vision_identify({"submission_id": "x", "images": [str(img)], "notes": None})

    for field in ("Brand", "ProductName", "InternalSubcategory"):
        assert field in seen["instructions"]


def test_classify_image_gemini_prompt_names_the_classify_schema_fields(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    seen = {}

    def fake_classify_json(instructions, image):
        seen["instructions"] = instructions
        return '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}'

    monkeypatch.setattr(gemini, "classify_json", fake_classify_json)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    adapters._classify_image(str(img), ledger=None, submission_id="x")

    instructions = seen["instructions"]
    # IMAGE_CLASSIFY_INSTRUCTIONS already names category/needs_cleanup/
    # clutter_bbox in its own prose, so asserting on those alone would pass
    # even with the schema dropped. Assert on the serialized schema itself.
    assert "Return JSON matching exactly this schema:" in instructions
    schema_text = instructions.split("Return JSON matching exactly this schema:")[1]
    for field in ("category", "needs_cleanup", "clutter_bbox", "required", "additionalProperties"):
        assert field in schema_text
    assert json.dumps(adapters._IMAGE_CLASSIFY_SCHEMA) in instructions


def test_generate_copy_openrouter_prompt_names_the_copy_schema_fields(monkeypatch):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_COPY_PROVIDER", "openrouter")
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )
    raw = {f: None for f in adapters.COPY_FIELDS}
    raw["MasterTitle"] = "Acme Smart Doorbell"
    seen = {}

    def fake_copy_json(instructions, facts_text):
        seen["instructions"] = instructions
        return json.dumps(raw)

    monkeypatch.setattr(openrouter, "copy_json", fake_copy_json)

    adapters.generate_copy(product)

    for field in adapters.COPY_FIELDS:
        assert field in seen["instructions"]


def test_retail_price_gemini_search_prompt_names_the_pricing_schema_fields(monkeypatch):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")
    raw = {"match_found": False, "anchor": None, "corroborating": None, "confidence_label": "Low"}
    seen = {}

    def fake_web_search_json(instructions):
        seen["instructions"] = instructions
        return json.dumps(raw)

    monkeypatch.setattr(gemini, "web_search_json", fake_web_search_json)

    adapters.retail_price({"submission_id": "x", "notes": None}, SOME_IDENTITY)

    instructions = seen["instructions"]
    # the per-call query text must survive alongside the appended schema
    assert "Smart Doorbell" in instructions
    for field in ("match_found", "anchor", "price_cad", "source_url", "original_currency"):
        assert field in instructions


def test_retail_price_prompt_config_matches_the_loaded_schema():
    """Literal expected values, not a reload-and-compare (see the note on
    test_image_classify_prompt_config_matches_the_loaded_constants)."""
    from resale_listing_ai import adapters

    assert adapters.RETAIL_PRICE_SCHEMA["required"] == ["match_found", "anchor", "corroborating", "confidence_label"]
    assert "{query}" in adapters._RETAIL_PRICE_TEMPLATE
    assert "never estimate or guess a price" in adapters._RETAIL_PRICE_TEMPLATE


def test_openai_vision_instructions_constant_is_not_mutated_by_schema_embedding():
    """The schema is appended to a COPY handed to the new providers; the module
    constants the OpenAI branches pass must stay clean, since those branches
    convey shape structurally and must behave byte-identically to before."""
    from resale_listing_ai import adapters

    for constant in (adapters.VISION_INSTRUCTIONS, adapters.COPY_INSTRUCTIONS,
                     adapters.IMAGE_CLASSIFY_INSTRUCTIONS):
        assert "Return JSON matching exactly this schema:" not in constant
        assert "properties" not in constant       # no serialized JSON schema
        assert "additionalProperties" not in constant


# ---------------------------------------------------------------------------
# Gemini transient errors — retried AND logged to the cost ledger
#
# _call_with_cost's default retry tuple is built from openai exception types
# only. OpenRouter reuses the openai SDK so it is covered for free, but
# google-genai raises its own hierarchy: without an explicit tuple a Gemini
# 429 propagates on the FIRST attempt with no ledger row at all, breaking the
# "log every call, including retries and failures" rule (project rule).
# ---------------------------------------------------------------------------

def test_gemini_transient_error_tuple_covers_the_real_sdk_exceptions():
    from google.genai import errors

    from resale_listing_ai import adapters

    # ClientError (4xx, incl. 429) and ServerError (5xx) must both match
    assert issubclass(errors.ClientError, adapters._GEMINI_TRANSIENT_ERRORS)
    assert issubclass(errors.ServerError, adapters._GEMINI_TRANSIENT_ERRORS)
    # and the openai types stay covered for the shared/OpenRouter paths
    for exc_type in adapters._TRANSIENT_ERRORS:
        assert issubclass(exc_type, adapters._GEMINI_RETRY_ERRORS)


def _gemini_rate_limit_error():
    """A real google-genai ClientError with a 429 status, built the way the SDK
    builds it — so this test breaks if that constructor shape ever changes."""
    from google.genai import errors

    return errors.ClientError(429, {"error": {"message": "rate limit", "status": "RESOURCE_EXHAUSTED"}})


def test_gemini_rate_limit_is_retried_and_every_attempt_hits_the_ledger(monkeypatch, tmp_path):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")

    def always_rate_limited(instructions, notes, images):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "vision_json", always_rate_limited)
    monkeypatch.setattr(adapters, "_stub_mode", lambda: False)

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    with pytest.raises(Exception):
        adapters.vision_identify(
            {"submission_id": "sub-429", "images": [str(img)], "notes": None}, ledger=ledger)

    # 1 initial attempt + 2 retries, each logged (the point of the fix)
    assert len(ledger.rows) == 3
    assert [r["service"] for r in ledger.rows] == ["gemini_vision"] * 3
    assert [r["retry"] for r in ledger.rows] == [False, True, True]


def test_gemini_copy_rate_limit_is_retried_and_logged(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_COPY_PROVIDER", "gemini")

    def always_rate_limited(instructions, facts_text):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "copy_json", always_rate_limited)
    product = _product_with(
        Brand={"value": "Acme", "confidence": 0.9, "source": "vision"},
        ProductName={"value": "Smart Doorbell", "confidence": 0.9, "source": "vision"},
    )
    ledger = CostLedger()

    with pytest.raises(Exception):
        adapters.generate_copy(product, ledger=ledger, submission_id="sub-429")

    assert len(ledger.rows) == 3
    assert all(r["service"] == "gemini_copy" for r in ledger.rows)


def test_gemini_search_persistent_rate_limit_degrades_retail_price_not_raises(monkeypatch):
    """A web-search provider 429 that persists through every retry is NOT fatal to
    the enrichment stage: retail_price degrades to the same all-NULL pricing map
    the no-match path returns (so ingest can still finish "Accepted with
    unknowns"), while every attempt is still logged to the ledger. It must NOT
    propagate -- that propagation was exactly what stranded a submission at
    RECEIVED and eventually dead-lettered it."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    def always_rate_limited(instructions):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "web_search_json", always_rate_limited)
    ledger = CostLedger()

    result = adapters.retail_price({"submission_id": "sub-429", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert result["PricingAnchorPriceCAD"]["value"] is None       # degraded, never fabricated
    assert result["LastPricingCheckDate"]["value"] is not None    # a check WAS attempted
    # retry logging is unchanged: 1 initial attempt + 2 retries, each billed
    assert len(ledger.rows) == 3
    assert all(r["service"] == "gemini_search" for r in ledger.rows)


def test_gemini_search_persistent_rate_limit_degrades_resale_price_not_raises(monkeypatch):
    """Same degradation for the resale (Comparable*) seam: a persistent provider
    429 yields an empty map (no comparable fields), not an exception."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    def always_rate_limited(instructions):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "web_search_json", always_rate_limited)
    ledger = CostLedger()

    result = adapters.resale_price({"submission_id": "sub-429", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert result == {}
    assert len(ledger.rows) == 3
    assert all(r["service"] == "gemini_search" for r in ledger.rows)


def test_gemini_search_persistent_rate_limit_degrades_measures_lookup_not_raises(monkeypatch):
    """measures_lookup's real-mode manufacturer-spec-sheet search rides the same
    web-search seam: a persistent provider 429 degrades to an empty map (no
    dimension fields), so ingest still finishes with the measurements simply
    null, rather than propagating and aborting the submission."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    def always_rate_limited(instructions):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "web_search_json", always_rate_limited)
    ledger = CostLedger()

    result = adapters.measures_lookup(
        {"submission_id": "sub-429", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert result == {}
    assert len(ledger.rows) == 3
    assert all(r["service"] == "gemini_search" for r in ledger.rows)


def test_gemini_search_persistent_rate_limit_degrades_image_match_not_raises(monkeypatch):
    """image_match shares the same web-search seam: a persistent provider 429
    degrades to a no-match result, never propagates."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    def always_rate_limited(instructions):
        raise _gemini_rate_limit_error()

    monkeypatch.setattr(gemini, "web_search_json", always_rate_limited)
    ledger = CostLedger()

    result = adapters.image_match({"submission_id": "sub-429", "notes": None}, SOME_IDENTITY, ledger=ledger)

    assert result == {"matched": False, "confidence": 0.0, "source": "none"}
    assert len(ledger.rows) == 3


# ---------------------------------------------------------------------------
# vision_identify's own cost row — provider-specific, not a flat $0.030
# ---------------------------------------------------------------------------

def test_vision_identify_accepts_an_optional_ledger():
    """Backward compatibility: a direct caller that doesn't care about cost
    must still be able to call vision_identify(submission) with one argument."""
    import inspect

    from resale_listing_ai import adapters

    params = inspect.signature(adapters.vision_identify).parameters
    assert params["ledger"].default is None


def test_vision_identify_stub_mode_logs_the_historical_flat_row(monkeypatch):
    """Stub-mode output must stay byte-identical: same stage, same generic
    'vision' service tag, same $0.030 -- just logged inside vision_identify
    now instead of by pipeline.py externally."""
    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    ledger = CostLedger()

    adapters.vision_identify({"submission_id": "x", "hint": "nest-doorbell"}, ledger=ledger)

    assert ledger.rows == [{
        "submission_id": "x", "stage": "identify", "service": "vision",
        "cost_cad": 0.030, "retry": False,
        "input_tokens": None, "output_tokens": None, "model": None,
    }]


def test_vision_identify_stub_mode_without_a_ledger_logs_nothing(monkeypatch):
    from resale_listing_ai import adapters

    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")

    identity = adapters.vision_identify({"submission_id": "x", "hint": "nest-doorbell"})

    assert identity["Brand"]["value"] == "Acme"


def test_vision_identify_logs_gemini_service_and_its_real_cost(monkeypatch, tmp_path):
    """The whole point of run_live_demo.py is an honest per-provider cost
    comparison -- Gemini's free tier must not report a paid provider's number
    under a generic 'vision' tag."""
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    monkeypatch.setattr(
        gemini, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_VISION_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.vision_identify(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["stage"] == "identify"
    assert ledger.rows[0]["service"] == "gemini_vision"
    assert ledger.rows[0]["cost_cad"] == gemini.GEMINI_UNIT_COST_CAD


def test_vision_identify_logs_openrouter_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "openrouter")
    monkeypatch.setattr(
        openrouter, "vision_json",
        lambda instructions, notes, images: json.dumps(RAW_VISION_OUTPUT))

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.vision_identify(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["service"] == "openrouter_vision"
    assert ledger.rows[0]["cost_cad"] == openrouter.OPENROUTER_UNIT_COST_CAD


def test_vision_identify_logs_openai_service_and_its_real_cost(monkeypatch, tmp_path):
    import json

    from resale_listing_ai import adapters
    from resale_listing_ai.cost import CostLedger

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_VISION_PROVIDER", raising=False)

    class _FakeResponses:
        def create(self, **kwargs):
            return type("R", (), {"output_text": json.dumps(RAW_VISION_OUTPUT)})()

    class _FakeClient:
        def __init__(self):
            self.responses = _FakeResponses()

    monkeypatch.setattr(adapters, "_get_openai_client", lambda: _FakeClient())

    img = tmp_path / "photo.jpg"
    img.write_bytes(Path(BARCODE_IMAGE).read_bytes())
    ledger = CostLedger()

    adapters.vision_identify(
        {"submission_id": "sub-1", "images": [str(img)], "notes": None}, ledger=ledger)

    assert ledger.rows[0]["service"] == "openai_vision"
    assert ledger.rows[0]["cost_cad"] == adapters.VISION_UNIT_COST_CAD


# ---------------------------------------------------------------------------
# _normalize_hero_bytes — hero output normalization
# ---------------------------------------------------------------------------

def test_normalize_hero_bytes_produces_2048_square_white_jpeg():
    from io import BytesIO
    from PIL import Image as PILImage
    from resale_listing_ai import adapters

    # a small non-square opaque source
    src = PILImage.new("RGB", (400, 200), (10, 20, 30))
    buf = BytesIO(); src.save(buf, format="PNG")

    out = adapters._normalize_hero_bytes(buf.getvalue())

    img = PILImage.open(BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (2048, 2048)
    assert img.mode == "RGB"
    # corner is white padding (JPEG is lossy, so allow a small tolerance)
    r, g, b = img.getpixel((5, 5))
    assert r > 248 and g > 248 and b > 248


# ---------------------------------------------------------------------------
# _generate_website_hero — text-first generation, QC gate, reference retry
# ---------------------------------------------------------------------------

_HERO_IDENTITY = {
    "Brand": {"value": "Acme", "confidence": 0.9, "source": "vision"},
    "ProductName": {"value": "Cordless Drill", "confidence": 0.9, "source": "vision"},
    "Colour": {"value": "Blue", "confidence": 0.8, "source": "vision"},
}
_PASS = {"branding_clear": True, "coherent": True, "matches_confirmed": True, "no_invented": True, "reasons": []}
_ACCURACY_FAIL = {"branding_clear": True, "coherent": True, "matches_confirmed": False, "no_invented": True, "reasons": ["wrong colour"]}
_BRANDING_FAIL = {"branding_clear": False, "coherent": True, "matches_confirmed": True, "no_invented": True, "reasons": ["logo visible"]}
_LOCAL_STORAGE = {"mode": "local", "client": None}


def _patch_hero_pipeline(monkeypatch, qc_results, generated=b"gen"):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini
    gen_calls = []
    # Pin the provider so these orchestration tests exercise the gemini fake
    # regardless of the configured default hero_generate provider.
    monkeypatch.setattr(adapters, "_hero_generate_provider", lambda config=None: "gemini")
    monkeypatch.setattr(gemini, "generate_hero_image",
                        lambda instructions, reference_bytes=None: gen_calls.append(reference_bytes) or generated)
    monkeypatch.setattr(adapters, "_normalize_hero_bytes", lambda b: b"normalized")
    qc_iter = iter(qc_results)
    monkeypatch.setattr(adapters, "_hero_qc", lambda *a, **k: next(qc_iter))
    monkeypatch.setattr(adapters, "_put_object", lambda storage, key, data, content_type="image/jpeg": f"file:///{key}")
    return gen_calls


def test_generate_website_hero_happy_path(monkeypatch):
    from resale_listing_ai import adapters
    gen_calls = _patch_hero_pipeline(monkeypatch, [_PASS])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env["value"] == "file:///finished/PK/website_hero.jpg"
    assert env["source"] == "generated"
    assert review is False
    assert rejected == ""  # nothing rejected on a clean pass
    assert gen_calls == [None]  # first attempt is text-only


def test_generate_website_hero_accuracy_fail_retries_with_reference(monkeypatch):
    from resale_listing_ai import adapters
    gen_calls = _patch_hero_pipeline(monkeypatch, [_ACCURACY_FAIL, _PASS])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env["value"].endswith("website_hero.jpg")
    assert review is False
    assert gen_calls == [None, b"scrubbed"]  # retry conditioned on the scrubbed ref


def test_generate_website_hero_branding_fail_flags_without_retry(monkeypatch):
    from resale_listing_ai import adapters
    gen_calls = _patch_hero_pipeline(monkeypatch, [_BRANDING_FAIL])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env == adapters.NULL()
    assert review is True
    assert gen_calls == [None]  # branding failure does not retry
    # the rejected image is saved and its URL returned for the admin review link
    assert rejected == "file:///finished/PK/website_hero_rejected.jpg"


def test_generate_website_hero_persistent_fail_flags(monkeypatch):
    from resale_listing_ai import adapters
    gen_calls = _patch_hero_pipeline(monkeypatch, [_ACCURACY_FAIL, _ACCURACY_FAIL])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env == adapters.NULL()
    assert review is True
    assert gen_calls == [None, b"scrubbed"]


def test_generate_website_hero_no_identity_flags(monkeypatch):
    from resale_listing_ai import adapters
    env, review, prompt, rejected = adapters._generate_website_hero(
        {}, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")
    assert env == adapters.NULL()
    assert review is True


def test_generate_website_hero_generation_error_degrades(monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    def boom(instructions, reference_bytes=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(gemini, "generate_hero_image", boom)

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env == adapters.NULL()
    assert review is True


def test_generate_website_hero_returns_the_generation_prompt(monkeypatch):
    from resale_listing_ai import adapters
    _patch_hero_pipeline(monkeypatch, [_PASS])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert review is False
    assert "Cordless Drill" in prompt  # depiction subject
    assert "Acme" in prompt  # identification context (present in prompt, fenced as do-not-render)
    assert "MUST NOT appear" in prompt  # the fence is in the built prompt


def test_generate_website_hero_returns_prompt_even_on_rejection(monkeypatch):
    from resale_listing_ai import adapters
    _patch_hero_pipeline(monkeypatch, [_BRANDING_FAIL])

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env == adapters.NULL() and review is True
    assert "Cordless Drill" in prompt  # prompt returned for audit even when rejected


def test_generate_website_hero_no_identity_returns_empty_prompt():
    from resale_listing_ai import adapters
    env, review, prompt, rejected = adapters._generate_website_hero(
        {}, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")
    assert env == adapters.NULL() and review is True and prompt == ""


def test_persist_hero_prompt_enabled_defaults_true(monkeypatch):
    from resale_listing_ai import adapters
    monkeypatch.delenv("RESALE_LISTING_AI_PERSIST_HERO_PROMPT", raising=False)
    assert adapters._persist_hero_prompt_enabled() is True


def test_persist_hero_prompt_disabled_via_env(monkeypatch):
    from resale_listing_ai import adapters
    monkeypatch.setenv("RESALE_LISTING_AI_PERSIST_HERO_PROMPT", "0")
    assert adapters._persist_hero_prompt_enabled() is False


def test_hero_depiction_excludes_brand_and_model_identification_includes_them():
    from resale_listing_ai import adapters
    identity = {
        "Brand": {"value": "AcmeCase", "confidence": 0.9, "source": "vision"},
        "ProductName": {"value": "Commuter Series Case", "confidence": 0.9, "source": "vision"},
        "ModelNumber": {"value": "AC-10001", "confidence": 0.9, "source": "vision"},
        "Colour": {"value": "Purple", "confidence": 0.9, "source": "vision"},
    }
    depiction = adapters._hero_depiction_text(identity)
    identification = adapters._hero_identification_text(identity)

    assert "Commuter Series Case" in depiction and "Purple" in depiction
    assert "AcmeCase" not in depiction and "AC-10001" not in depiction  # brand/model never in the drawn subject
    assert "AcmeCase" in identification and "AC-10001" in identification


def test_generate_website_hero_logs_reasons_and_saves_rejected(monkeypatch, capsys):
    from resale_listing_ai import adapters
    _patch_hero_pipeline(monkeypatch, [_BRANDING_FAIL])
    puts = []
    monkeypatch.setattr(
        adapters, "_put_object",
        lambda storage, key, data, content_type="image/jpeg": puts.append(key) or f"file:///{key}")

    env, review, prompt, rejected = adapters._generate_website_hero(
        _HERO_IDENTITY, "PK", _LOCAL_STORAGE, b"scrubbed", ledger=None, submission_id="s")

    assert env == adapters.NULL() and review is True
    err = capsys.readouterr().err
    assert "QC rejected PK" in err
    assert "branding_clear" in err  # names the failed check
    assert any("website_hero_rejected.jpg" in k for k in puts)  # rejected image saved for inspection
