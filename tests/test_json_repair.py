"""Tests for resale_listing_ai/json_repair.py — the schema-coercion + LLM-fallback repair
step wired into each adapter's raw-JSON stage boundary."""

import json

import pytest

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "status": {"type": "string", "enum": ["ok", "unknown"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "detail": {
            "type": ["object", "null"],
            "properties": {"note": {"type": ["string", "null"]}},
            "required": ["note"],
            "additionalProperties": False,
        },
    },
    "required": ["name", "confidence", "status", "tags", "detail"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# _try_parse_json — local, free JSON parsing (with/without a ```json fence)
# ---------------------------------------------------------------------------

def test_try_parse_json_parses_plain_json_string():
    from resale_listing_ai.json_repair import _try_parse_json

    assert _try_parse_json('{"a": 1}') == {"a": 1}


def test_try_parse_json_strips_markdown_code_fence():
    from resale_listing_ai.json_repair import _try_parse_json

    assert _try_parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_try_parse_json_returns_none_for_prose():
    from resale_listing_ai.json_repair import _try_parse_json

    assert _try_parse_json("Looks like a used doorbell, hard to say more.") is None


def test_try_parse_json_passes_through_already_parsed_dict():
    from resale_listing_ai.json_repair import _try_parse_json

    assert _try_parse_json({"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# _coerce_to_schema — pure schema projection, the "never introduces a value" core
# ---------------------------------------------------------------------------

def test_coerce_to_schema_passes_through_well_formed_input():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"name": "Smart Doorbell", "confidence": 0.9, "status": "ok",
             "tags": ["doorbell", "wired"], "detail": {"note": "2nd gen"}}

    assert _coerce_to_schema(value, SCHEMA) == value


def test_coerce_to_schema_nulls_a_field_missing_from_the_source():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"confidence": 0.5, "status": "ok", "tags": [], "detail": None}

    coerced = _coerce_to_schema(value, SCHEMA)

    assert coerced["name"] is None  # never fabricated -> stays null


def test_coerce_to_schema_nulls_an_invalid_enum_value_never_keeps_it():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"name": "x", "confidence": 0.5, "status": "definitely-maybe",
             "tags": [], "detail": None}

    coerced = _coerce_to_schema(value, SCHEMA)

    assert coerced["status"] is None


def test_coerce_to_schema_nulls_a_wrong_typed_value():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"name": 12345, "confidence": "not-a-number", "status": "ok",
             "tags": [], "detail": None}

    coerced = _coerce_to_schema(value, SCHEMA)

    assert coerced["name"] is None
    assert coerced["confidence"] is None


def test_coerce_to_schema_drops_keys_not_declared_in_the_schema():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": None,
             "invented_field": "the model made this up"}

    coerced = _coerce_to_schema(value, SCHEMA)

    assert "invented_field" not in coerced


def test_coerce_to_schema_recurses_into_nested_objects():
    from resale_listing_ai.json_repair import _coerce_to_schema

    value = {"name": "x", "confidence": 0.5, "status": "ok", "tags": [],
             "detail": {"note": "fine", "extra": "dropped"}}

    coerced = _coerce_to_schema(value, SCHEMA)

    assert coerced["detail"] == {"note": "fine"}


# ---------------------------------------------------------------------------
# missing_field_reasons — null + a reason for anything missing
# ---------------------------------------------------------------------------

def test_missing_field_reasons_flags_absent_field_as_missing():
    from resale_listing_ai.json_repair import missing_field_reasons

    reasons = missing_field_reasons({"confidence": 0.5, "status": "ok", "tags": [], "detail": None}, SCHEMA)

    assert reasons["name"] == "missing"


def test_missing_field_reasons_flags_bad_enum_and_bad_type_distinctly():
    from resale_listing_ai.json_repair import missing_field_reasons

    value = {"name": 5, "confidence": 0.5, "status": "nope", "tags": [], "detail": None}

    reasons = missing_field_reasons(value, SCHEMA)

    assert reasons["name"] == "invalid_type"
    assert reasons["status"] == "invalid_enum"


def test_missing_field_reasons_has_no_entry_for_valid_fields():
    from resale_listing_ai.json_repair import missing_field_reasons

    value = {"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": None}

    reasons = missing_field_reasons(value, SCHEMA)

    assert "name" not in reasons
    assert "status" not in reasons


# ---------------------------------------------------------------------------
# repair_json — local fast path never calls any LLM for well-formed input
# ---------------------------------------------------------------------------

def test_repair_json_well_formed_input_never_calls_an_llm(monkeypatch):
    from resale_listing_ai import json_repair

    def _boom():
        raise AssertionError("must not call any LLM when the raw text already parses cleanly")

    monkeypatch.setattr(json_repair, "_get_anthropic_client", _boom)
    monkeypatch.setattr(json_repair, "_get_openai_client", _boom)

    raw = '{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": null}'
    result = json_repair.repair_json(raw, SCHEMA, "thing")

    assert result["name"] == "x"


def test_repair_json_unwraps_a_single_element_array_wrapping_the_object(monkeypatch):
    """Some vision models (observed with Gemini/Gemma) wrap a well-formed object
    answer in a one-element JSON array, e.g. `[{...}]` instead of `{...}`. That's
    still valid JSON, so it must not silently coerce to an all-null object -- the
    real values inside the wrapper must survive."""
    from resale_listing_ai import json_repair

    def _boom():
        raise AssertionError("must not call any LLM when the raw text already parses cleanly")

    monkeypatch.setattr(json_repair, "_get_anthropic_client", _boom)
    monkeypatch.setattr(json_repair, "_get_openai_client", _boom)

    raw = json.dumps([{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": None}])
    result = json_repair.repair_json(raw, SCHEMA, "thing")

    assert result["name"] == "x"


def test_repair_json_well_formed_input_drops_undeclared_fields_without_an_llm(monkeypatch):
    from resale_listing_ai import json_repair

    def _boom():
        raise AssertionError("must not call any LLM when the raw text already parses cleanly")

    monkeypatch.setattr(json_repair, "_get_anthropic_client", _boom)
    monkeypatch.setattr(json_repair, "_get_openai_client", _boom)

    raw = json.dumps({"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": None,
                       "made_up": "should never survive"})
    result = json_repair.repair_json(raw, SCHEMA, "thing")

    assert "made_up" not in result


# ---------------------------------------------------------------------------
# repair_json — Claude fallback for genuinely unparseable raw text
# ---------------------------------------------------------------------------

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


def test_repair_json_falls_back_to_claude_for_unparseable_raw_text(monkeypatch):
    from resale_listing_ai import json_repair

    fake_client = _FakeAnthropicClient(
        '```json\n{"name": "Smart Doorbell", "confidence": 0.7, "status": "ok", '
        '"tags": ["doorbell"], "detail": null}\n```')
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_client)

    result = json_repair.repair_json("prose: it's a Nest doorbell, kind of used", SCHEMA, "thing")

    assert fake_client.messages.last_call is not None
    assert result["name"] == "Smart Doorbell"


def test_repair_json_openai_logs_token_based_cost(monkeypatch):
    """When the OpenAI repair call returns usage, its ledger row bills the real
    token cost (config/model_rates.json) and records tokens+model, instead of
    the flat JSON_REPAIR_UNIT_COST_CAD."""
    from types import SimpleNamespace
    from resale_listing_ai import json_repair
    from resale_listing_ai.cost import CostLedger

    monkeypatch.setenv("RESALE_LISTING_AI_JSON_REPAIR_PROVIDER", "openai")
    monkeypatch.setattr(json_repair, "OPENAI_REPAIR_MODEL", "gpt-4o-mini")

    class _Responses:
        def create(self, **kwargs):
            return SimpleNamespace(
                output_text='{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": null}',
                usage=SimpleNamespace(input_tokens=1000, output_tokens=500))

    monkeypatch.setattr(json_repair, "_get_openai_client",
                        lambda: SimpleNamespace(responses=_Responses()))

    ledger = CostLedger()
    json_repair.repair_json("garbled", SCHEMA, "thing", ledger=ledger, submission_id="s1")

    row = ledger.rows[0]
    assert row["model"] == "gpt-4o-mini"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cost_cad"] == 0.0006  # token cost, not the flat 0.01


def test_repair_json_claude_fallback_still_drops_a_fabricated_extra_field(monkeypatch):
    """Even if the repair model itself invents an extra field, the post-coercion
    pass strips anything not declared in the schema — a code-level backstop, not
    just a prompt instruction."""
    from resale_listing_ai import json_repair

    fake_client = _FakeAnthropicClient(
        '```json\n{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], '
        '"detail": null, "invented_serial_number": "SN-12345"}\n```')
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_client)

    result = json_repair.repair_json("garbled text", SCHEMA, "thing")

    assert "invented_serial_number" not in result


def test_repair_json_claude_fallback_keeps_null_when_the_model_declines(monkeypatch):
    from resale_listing_ai import json_repair

    fake_client = _FakeAnthropicClient(
        '```json\n{"name": null, "confidence": 0.2, "status": "unknown", "tags": [], '
        '"detail": null}\n```')
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_client)

    result = json_repair.repair_json("totally illegible scrawl", SCHEMA, "thing")

    assert result["name"] is None  # the model said it doesn't know -> stays null, not guessed


def test_repair_json_logs_cost_only_when_the_llm_path_is_used(monkeypatch):
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai import json_repair

    fake_client = _FakeAnthropicClient(
        '```json\n{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": null}\n```')
    monkeypatch.setattr(json_repair, "_get_anthropic_client", lambda: fake_client)

    ledger = CostLedger()
    well_formed = '{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": null}'
    json_repair.repair_json(well_formed, SCHEMA, "thing", ledger=ledger, submission_id="s1", stage="identify")
    assert ledger.rows == []  # local fast path -> no cost

    json_repair.repair_json("garbled", SCHEMA, "thing", ledger=ledger, submission_id="s1", stage="identify")
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["service"] == "claude_json_repair"
    assert ledger.rows[0]["stage"] == "identify"
    assert ledger.rows[0]["submission_id"] == "s1"


def test_json_repair_provider_uses_admin_override(monkeypatch):
    from resale_listing_ai import json_repair

    monkeypatch.delenv("RESALE_LISTING_AI_JSON_REPAIR_PROVIDER", raising=False)
    assert json_repair._json_repair_provider() == "claude"  # file default
    assert json_repair._json_repair_provider(config={"provider_stack.json_repair_provider": "openai"}) == "openai"


def test_repair_json_uses_openai_provider_when_configured(monkeypatch):
    from resale_listing_ai import json_repair

    monkeypatch.setenv("RESALE_LISTING_AI_JSON_REPAIR_PROVIDER", "openai")

    class _FakeResponse:
        def __init__(self, text):
            self.output_text = text

    class _FakeResponses:
        def __init__(self, text):
            self._text = text
            self.last_call = None

        def create(self, **kwargs):
            self.last_call = kwargs
            return _FakeResponse(self._text)

    class _FakeOpenAIClient:
        def __init__(self, text):
            self.responses = _FakeResponses(text)

    fake_client = _FakeOpenAIClient(
        '{"name": "x", "confidence": 0.5, "status": "ok", "tags": [], "detail": null}')
    monkeypatch.setattr(json_repair, "_get_openai_client", lambda: fake_client)

    result = json_repair.repair_json("garbled", SCHEMA, "thing")

    assert fake_client.responses.last_call is not None
    assert result["name"] == "x"


def test_repair_json_requires_api_key_for_selected_provider(monkeypatch):
    from resale_listing_ai import json_repair

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        json_repair.repair_json("garbled beyond parsing", SCHEMA, "thing")
