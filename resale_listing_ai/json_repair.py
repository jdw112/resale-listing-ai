"""JSON-repair / normalizer — coerces raw (possibly messy) model output onto one
of our schemas at each stage boundary, using a repair prompt applied to OUR real
adapter schemas.

Two passes, always in this order, both free of a network call in the common case:
  1. LOCAL coercion (_coerce_to_schema) — parse the raw text/dict, then walk the
     given JSON-schema and copy through only values that already exist, type/enum-
     validate, and are declared in the schema; everything else -> null. This is
     the mechanism that makes "never introduces a value" true in code, not just
     in a prompt: the output can only ever contain data present in the input.
  2. LLM fallback — only when the local parse fails outright (raw isn't valid JSON
     at all, e.g. prose). Provider is selectable (config/provider_stack.json's
     json_repair_provider, override RESALE_LISTING_AI_JSON_REPAIR_PROVIDER) — not tied to
     one vendor. Whichever model answers, its response is re-run through the SAME
     local coercion before being trusted, so even a fabricated extra field or an
     out-of-schema value never survives.
"""

import json
import os
import re
from pathlib import Path

from . import admin_config
from . import rates

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_PROVIDER_STACK = json.loads((CONFIG_DIR / "provider_stack.json").read_text())

CLAUDE_REPAIR_MODEL = os.environ.get("RESALE_LISTING_AI_CLAUDE_REPAIR_MODEL", "claude-haiku-4-5-20251001")
OPENAI_REPAIR_MODEL = os.environ.get("RESALE_LISTING_AI_OPENAI_REPAIR_MODEL", "gpt-4o-mini")
JSON_REPAIR_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_JSON_REPAIR_UNIT_COST_CAD", "0.01"))

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


def _json_repair_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_JSON_REPAIR_PROVIDER", _PROVIDER_STACK.get("json_repair_provider", "claude"))
    return admin_config.get(config, "provider_stack.json_repair_provider", default)


def _try_parse_json(raw):
    """Local, free, deterministic. Returns a parsed dict/list, or None if `raw`
    isn't valid JSON (optionally wrapped in a ```json fence)."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _coerce_to_schema(value, schema):
    """Recursively project `value` onto `schema` (a JSON-Schema-shaped dict, the
    same shape already used throughout resale_listing_ai/adapters.py). Anything missing,
    wrongly typed, not a recognized enum value, or not declared in the schema
    becomes None — never guessed, never carried through unchecked."""
    declared = schema.get("type")
    allowed = declared if isinstance(declared, list) else [declared]
    nullable = "null" in allowed

    if "object" in allowed:
        props = schema.get("properties", {})
        if isinstance(value, list) and len(value) == 1:
            value = value[0]  # some models wrap a well-formed object in `[{...}]`
        source = value if isinstance(value, dict) else {}
        if not isinstance(value, dict) and nullable:
            return None
        return {key: _coerce_to_schema(source.get(key), sub) for key, sub in props.items()}

    if "array" in allowed:
        if not isinstance(value, list):
            return None if nullable else []
        items_schema = schema.get("items")
        return [_coerce_to_schema(v, items_schema) for v in value] if items_schema else list(value)

    if "boolean" in allowed:
        return value if isinstance(value, bool) else (None if nullable else False)

    if "number" in allowed or "integer" in allowed:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return None

    if "string" in allowed:
        if isinstance(value, str) and value.strip():
            enum = schema.get("enum")
            if enum and value not in enum:
                return None
            return value
        return None

    return None


def missing_field_reasons(original, schema):
    """For a schema's top-level properties, why each ended up null: 'missing' (key
    absent from `original`), 'invalid_type' or 'invalid_enum' (present but didn't
    validate). No entry for a field with a real value. Companion to repair_json for
    audit/debugging — never used to fabricate a value, only to explain a null."""
    source = original if isinstance(original, dict) else {}
    reasons = {}
    for key, sub_schema in schema.get("properties", {}).items():
        if key not in source:
            reasons[key] = "missing"
            continue
        raw_value = source[key]
        if raw_value is None:
            continue  # explicitly null in the source -> not a repair problem
        if _coerce_to_schema(raw_value, sub_schema) is None:
            reasons[key] = "invalid_enum" if sub_schema.get("enum") else "invalid_type"
    return reasons


def _get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set (json_repair_provider=claude). Set it in the "
            "environment, switch RESALE_LISTING_AI_JSON_REPAIR_PROVIDER to openai, or set "
            "RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)


def _get_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set (json_repair_provider=openai). Set it in the "
            "environment, switch RESALE_LISTING_AI_JSON_REPAIR_PROVIDER to claude, or set "
            "RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _repair_prompt(raw, schema, schema_name):
    return (
        "Act as a strict JSON Data Sanitizer. Map the RAW AI OUTPUT below onto the "
        "TARGET JSON SCHEMA field paths exactly. Standardize field names, types, "
        "nesting, and enum values to match the schema. If a field is missing, "
        "unreadable, or ambiguous, set it to null — never guess, infer, or "
        "hallucinate a value not actually supported by the raw output. Return ONLY "
        "valid JSON matching the schema, wrapped in a ```json code block, nothing "
        f"else.\n\nTARGET JSON SCHEMA ({schema_name}):\n{json.dumps(schema)}\n\n"
        f"RAW AI OUTPUT:\n{raw}"
    )


def _usage(response, model):
    """Token usage from an OpenAI Responses or Anthropic Messages result (both
    expose input_tokens/output_tokens on .usage) as the pricing dict, or None
    when absent so cost falls back to the flat unit cost."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    return {"model": model,
            "input_tokens": getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None)}


def _claude_repair_text(raw, schema, schema_name):
    client = _get_anthropic_client()
    response = client.messages.create(
        model=CLAUDE_REPAIR_MODEL, max_tokens=2048,
        messages=[{"role": "user", "content": _repair_prompt(raw, schema, schema_name)}],
    )
    text = "".join(getattr(block, "text", "") for block in response.content)
    return text, _usage(response, CLAUDE_REPAIR_MODEL)


def _openai_repair_text(raw, schema, schema_name):
    client = _get_openai_client()
    response = client.responses.create(
        model=OPENAI_REPAIR_MODEL,
        input=[{"role": "user", "content": [{"type": "input_text", "text": _repair_prompt(raw, schema, schema_name)}]}],
    )
    return response.output_text, _usage(response, OPENAI_REPAIR_MODEL)


def _log_and_call(ledger, submission_id, stage, service, unit_cost_cad, fn):
    """Call fn(), which returns (text, usage). Bill the real token cost when
    usage is present and the model is priced (config/model_rates.json), else
    the flat unit_cost_cad; token counts + model are recorded either way."""
    text, usage = fn()
    if ledger is not None:
        cost, tokens = unit_cost_cad, {}
        if usage:
            tokens = {"input_tokens": usage.get("input_tokens"),
                      "output_tokens": usage.get("output_tokens"), "model": usage.get("model")}
            token_cost = rates.token_cost_cad(
                usage.get("model"), usage.get("input_tokens"), usage.get("output_tokens"))
            if token_cost is not None:
                cost = token_cost
        ledger.add(submission_id, stage, service, cost, **tokens)
    return text


def repair_json(raw, schema, schema_name, *, ledger=None, submission_id=None, stage=None, config=None):
    """Coerce `raw` (a JSON string, a ```-fenced JSON string, or an already-parsed
    dict/list) onto `schema`. Never fabricates a value: anything missing or
    unparseable becomes null. Only calls out to an LLM when `raw` isn't valid JSON
    at all — the common well-formed case never touches the network."""
    parsed = _try_parse_json(raw)
    if parsed is not None:
        return _coerce_to_schema(parsed, schema)

    if _json_repair_provider(config) == "openai":
        service, fn = "openai_json_repair", lambda: _openai_repair_text(raw, schema, schema_name)
    else:
        service, fn = "claude_json_repair", lambda: _claude_repair_text(raw, schema, schema_name)

    repaired_text = _log_and_call(ledger, submission_id, stage or "repair", service, JSON_REPAIR_UNIT_COST_CAD, fn)
    return _coerce_to_schema(_try_parse_json(repaired_text) or {}, schema)
