"""Adapter interfaces + implementations.

Each adapter is where a real provider is called: `barcode_read`, `vision_identify`,
`image_match`, `retail_price`, `resale_price`, `image_process`, and `generate_copy`
all call real providers — see below. Set RESALE_LISTING_AI_STUBS=1 to force every adapter
(including the real ones) back to the offline mock path — this is how the existing
test suite and demo keep running with no API keys/network/zbar.

Every real adapter's raw model output is run through `json_repair.repair_json`
before mapping — a schema-coercion pass (free, local) that nulls anything missing/
malformed and drops anything not declared in the schema, falling back to an LLM
(provider-selectable, see json_repair.py) only when the raw text isn't valid JSON
at all. This is the "JSON-repair/normalizer at each stage boundary" — see
json_repair.py's module docstring for the full design.

Decided provider stack:
  vision_identify -> OpenAI Vision                                   [REAL, below]
  barcode_read    -> pyzbar / ZXing (deterministic, not a model call) [REAL, below]
  ocr_read        -> pytesseract / Tesseract (deterministic, not a model call,
                     feeds vision_identify as context only)          [REAL, below]
  image_match  \
  retail_price  } -> ONE web-search-with-citations model, selectable in            [REAL,
  resale_price /     config/provider_stack.json (default: OpenAI Responses          below]
                     `web_search` tool; also supports Perplexity Sonar).
                     Sidesteps the gated Google Lens / Amazon / eBay-sold APIs;
                     returns CAD + source URLs. Dedicated APIs are optional upgrades.
  image_process   -> OpenAI Vision classifies each image (photo category + tape/
                     ruler/clutter flag + bbox) -> PhotoRoom removes the background
                     ($0.02/call) -> Clipdrop Cleanup inpaints ONLY the flagged
                     bbox, never the whole frame -> S3 (auto-falls-back to local
                     disk, RESALE_LISTING_AI_LOCAL_IMAGE_DIR, when S3 isn't reachable).
                     Three sub-calls behind one adapter seam; nothing else in
                     the pipeline changes.                                     [REAL, below]
  generate_copy   -> OpenAI, given ONLY confident() Product facts               [REAL, below]
  JSON repair     -> selectable in config/provider_stack.json's json_repair_provider
                     (default Claude; also supports OpenAI) — see json_repair.py
One OpenAI key can cover vision + web-search + copy + image classification. Scope
pricing prompts to Canadian retail so the anchor is genuinely CAD; keep the
returned citation URLs."""

import json
import mimetypes
import os
import sys
from base64 import b64encode
from pathlib import Path

from . import admin_config
from . import rates
from .envelope import E, NULL, THRESHOLD, confident
from .json_repair import repair_json

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# Where uploaded images live. Image refs from the intake API (api.py's _write_uploads)
# are bare filenames stored under this root; the adapters that read image bytes must
# resolve them here, since the worker's CWD is not the uploads dir. Mirrors
# api.py's UPLOADS_ROOT / RESALE_LISTING_AI_UPLOADS_DIR.
UPLOADS_ROOT = Path(os.environ.get("RESALE_LISTING_AI_UPLOADS_DIR") or (Path(__file__).resolve().parents[1] / "uploads"))


def _resolve_image_path(image_path):
    """Resolve an image reference to a real filesystem path. An absolute path or a
    path that already exists as given (e.g. run_live_demo's `uploads/photo1.jpg`) is
    used as-is; a bare API ref (`<uuid>.jpg`) is resolved under UPLOADS_ROOT."""
    p = Path(image_path)
    if p.is_absolute() or p.exists():
        return p
    return UPLOADS_ROOT / image_path
PROMPTS_DIR = CONFIG_DIR / "prompts"


def _load_prompt(name):
    path = PROMPTS_DIR / f"{name}.json"
    try:
        prompt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"config/prompts/{name}.json could not be loaded: {exc}") from exc
    if "instructions" not in prompt:
        raise ValueError(f"config/prompts/{name}.json is missing required key 'instructions'")
    return prompt


def _validate_strict_schema(schema, prompt_name):
    """Recursively enforce OpenAI strict-mode invariants (every object node --
    including nullable ones written as {"type": ["object", "null"]} -- declares
    additionalProperties: false and required == exactly its properties keys)
    -- fails at import time with a clear error naming the prompt and node,
    instead of a cryptic 400 the first time it's actually called."""
    def _walk(node, path):
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if ("object" in types or node_type is None) and "properties" in node:
            props, required = set(node["properties"]), set(node.get("required", []))
            if node.get("additionalProperties") is not False:
                raise ValueError(f"{prompt_name}: {path} must set additionalProperties: false")
            if required != props:
                raise ValueError(f"{prompt_name}: {path} required {sorted(required)} != properties {sorted(props)}")
            for key, sub in node["properties"].items():
                _walk(sub, f"{path}.{key}")
        elif isinstance(node.get("items"), dict):
            _walk(node["items"], f"{path}[]")
    _walk(schema, "root")

# ---- real-adapter configuration ------------------------------------------------
VISION_MODEL = os.environ.get("RESALE_LISTING_AI_VISION_MODEL", "gpt-4o-mini")
# Nominal per-call estimate for the OpenAI vision-identify call; tune from real
# usage. Default matches the flat $0.030 identify row stub mode has always
# logged. Gemini/OpenRouter use their own provider unit costs instead.
VISION_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_VISION_UNIT_COST_CAD", "0.030"))

# UPC/EAN symbologies only — a QR code or Code128 on a shipping label is not a
# product barcode and must not be reported as one.
BARCODE_TYPES = {"EAN13", "EAN8", "UPCA", "UPCE"}

# Product identity fields the vision call extracts, in the exact Product schema
# field names (config/product_schema.json) so downstream code can assign them
# straight into the Product record with no further translation. Instructions,
# fields, and field_schema live in config/prompts/vision_identify.json.
_VISION_PROMPT = _load_prompt("vision_identify")
IDENTITY_FIELDS = _VISION_PROMPT["fields"]
_VISION_FIELD_SCHEMA = _VISION_PROMPT["field_schema"]
VISION_JSON_SCHEMA = {
    "type": "object",
    "properties": {f: _VISION_FIELD_SCHEMA for f in IDENTITY_FIELDS},
    "required": IDENTITY_FIELDS,
    "additionalProperties": False,
}
_validate_strict_schema(VISION_JSON_SCHEMA, "vision_identify")
VISION_INSTRUCTIONS = _VISION_PROMPT["instructions"]

# Listing-side "observed physical unit" fields the vision call extracts, in the
# exact Listing schema field names (config/listing_schema.json) — condition,
# completeness, defects, and testing, as distinct from vision_identify's
# product-identity fields above. Instructions, fields, and field_schema live in
# config/prompts/unit_observe.json.
_UNIT_PROMPT = _load_prompt("unit_observe")
UNIT_FIELDS = _UNIT_PROMPT["fields"]
_UNIT_FIELD_SCHEMA = _UNIT_PROMPT["field_schema"]
UNIT_JSON_SCHEMA = {
    "type": "object",
    "properties": {f: _UNIT_FIELD_SCHEMA for f in UNIT_FIELDS},
    "required": UNIT_FIELDS,
    "additionalProperties": False,
}
_validate_strict_schema(UNIT_JSON_SCHEMA, "unit_observe")
UNIT_INSTRUCTIONS = _UNIT_PROMPT["instructions"]

# ---- a small "world" the stubs know about, keyed by a submission hint ----------
_KNOWN = {
    "nest-doorbell": {
        "barcode": None,  # no readable UPC in this sample
        "identity": {
            "Brand": E("Acme", 0.92, "vision"),
            "Manufacturer": E("Acme Corp", 0.90, "external_lookup"),
            "ProductName": E("Smart Doorbell (wired, 2nd gen)", 0.90, "vision"),
            "ProductType": E("Wired video doorbell", 0.85, "vision"),
            "ProductLineOrSeries": E("Smart Doorbell", 0.85, "vision"),
            "ModelNumber": E("AC1000-XY", 0.94, "label_ocr"),
            "MPN": E("AC1000-XY", 0.94, "label_ocr"),
            "InternalCategory": E("Smart Home", 0.88, "vision"),
            "InternalSubcategory": E("Video Doorbells", 0.86, "vision"),
            "Variant": E("Wired, 2nd generation", 0.8, "vision"),
            "Colour": E("Ash", 0.80, "vision"),
            "Material": E("Plastic", 0.75, "vision"),
        },
        "measures": {
            "UnpackagedLengthCM": E(2.8, 0.7, "external_lookup"),
            "UnpackagedWidthCM": E(4.2, 0.7, "external_lookup"),
            "UnpackagedHeightCM": E(13.1, 0.7, "external_lookup"),
            "UnpackagedWeightKG": E(0.14, 0.7, "external_lookup"),
            # packaged dims unknown -> OversizedFlag becomes "Unable to Determine"
        },
        "pricing": {
            "PricingAnchorPriceCAD": E(149.99, 0.6, "external_lookup"),
            "PricingAnchorSourceName": E("The Home Depot Canada", 0.6, "external_lookup"),
            "PricingAnchorSourceURL": E("https://www.homedepot.ca/...", 0.6, "external_lookup"),
            "PricingConfidence": E("Low", 0.6, "derived"),
            "LastPricingCheckDate": E("2026-07-24", 1.0, "derived"),
        },
        "unit": {  # what the processor observed on THIS physical unit
            "SerialNumber": E("SN-AC1000-8841", 0.9, "label_ocr"),
            "AISuggestedConditionGrade": E("Open Box - Like New", 0.8, "vision"),
            "ActualIncludedItems": E("Doorbell, base plate, wedge, chime puck, hex key", 0.8, "vision"),
            "OriginalPackagingIncluded": E("Yes", 0.85, "vision"),
            "OriginalBoxIncluded": E("Yes", 0.85, "vision"),
            "MissingHardware": E("No", 0.8, "vision"),
            "DefectsOrDamage": E("No visible cracks or lens damage", 0.75, "vision"),
        },
    },
}


# ---- adapter functions ---------------------------------------------------------
def _stub_mode():
    return os.environ.get("RESALE_LISTING_AI_STUBS") == "1"


def _stub_barcode_read(submission):
    hint = submission.get("hint")
    upc = _KNOWN.get(hint, {}).get("barcode")
    return E(upc, 0.99, "barcode") if upc else NULL()


def _stub_vision_identify(submission):
    """Return an identity dict of envelopes. Unknown items -> weak/blank identity."""
    hint = submission.get("hint")
    if hint in _KNOWN:
        return dict(_KNOWN[hint]["identity"])
    # unrecognized: low-confidence, mostly null -> the gate will stop it
    return {
        "Brand": E("(unbranded)", 0.28, "vision"),
        "ProductName": NULL(),
        "ProductType": E("unknown part", 0.2, "vision"),
        "InternalCategory": NULL(),
        "ModelNumber": NULL(),
    }


def barcode_read(submission):
    """Decode a UPC/EAN barcode from the submission's images with pyzbar
    (deterministic, no model call, no cost). Reads every image and returns the
    first UPC/EAN symbol found; an unreadable or absent barcode returns NULL —
    it is never guessed or autocorrected to a plausible-looking number."""
    if _stub_mode():
        return _stub_barcode_read(submission)

    from PIL import Image, UnidentifiedImageError
    from pyzbar.pyzbar import decode as zbar_decode

    for image in submission.get("images", []):
        try:
            img = image if hasattr(image, "size") else Image.open(_resolve_image_path(image))
        except (OSError, UnidentifiedImageError, FileNotFoundError):
            continue  # unreadable/missing image -> skip it, never guess
        for symbol in zbar_decode(img):
            if symbol.type in BARCODE_TYPES:
                return E(symbol.data.decode("ascii"), 0.98, "barcode")
    return NULL()


_OCR_MAX_CHARS_PER_IMAGE = 1500


def _stub_ocr_read(submission):
    return ""


def ocr_read(submission):
    """Extract printed label/rating-plate text from the submission's images
    locally with pytesseract/Tesseract (deterministic, local, no cost, no
    model call) -- same dependency shape as barcode_read's pyzbar. Feeds
    vision_identify as extra prompt context; NEVER populates a Product field
    on its own, so unlike every other adapter in this file it returns a plain
    string, not an envelope. Reads every image, skips anything unreadable
    (never raises, never guesses), and truncates each image's extracted text
    to _OCR_MAX_CHARS_PER_IMAGE before joining -- real vision providers bill
    by token, so this bounds prompt size even though this stage's ledger row
    is a flat $0.000."""
    if _stub_mode():
        return _stub_ocr_read(submission)

    from PIL import Image
    import pytesseract

    blocks = []
    for image in submission.get("images", []):
        try:
            img = image if hasattr(image, "size") else Image.open(_resolve_image_path(image))
            text = pytesseract.image_to_string(img).strip()
        except Exception:
            continue  # unreadable image / missing tesseract binary -> skip, never guess
        if text:
            blocks.append(text[:_OCR_MAX_CHARS_PER_IMAGE])
    return "\n\n".join(blocks)


def _get_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Set it in the environment for real vision "
            "calls, or set RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _image_data_url(image_path):
    resolved = _resolve_image_path(image_path)
    data = resolved.read_bytes()
    mime = mimetypes.guess_type(str(resolved))[0] or "image/jpeg"
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def _instructions_with_ocr(instructions, ocr_text):
    """Fold locally-extracted OCR text into the vision prompt as a distinct,
    clearly labeled context block -- kept separate from the submitter's own
    `notes` (source "context") so the model isn't left guessing whether a
    given fact came from the submitter or from a label the camera saw."""
    if not ocr_text:
        return instructions
    return (
        instructions
        + "\n\nOCR-extracted label text (read locally from the submission's "
        "photos -- may be noisy or incomplete; weigh it as a hint alongside "
        "what you see in the images, not as ground truth):\n"
        + ocr_text
    )


# Models sometimes emit the literal string "null" (or "n/a") as a field value
# instead of a JSON null. Treat those as blank so they don't get stored as the
# text "null". Deliberately NOT including "none"/"unknown" — those can be
# legitimate answers (e.g. MissingItems = "None").
_BLANK_VALUE_TOKENS = {"null", "n/a", "na", "nil"}


def _is_blank_value(v):
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in _BLANK_VALUE_TOKENS:
        return True
    return v == ""


def _map_vision_output(raw):
    """Map the model's raw {field: {value, confidence, source}} JSON onto the
    {value, confidence, source} envelope, keyed by real Product schema field
    names. Missing/blank/malformed entries -> NULL, never fabricated."""
    out = {}
    for field in IDENTITY_FIELDS:
        item = raw.get(field) if isinstance(raw, dict) else None
        if not isinstance(item, dict) or _is_blank_value(item.get("value")):
            out[field] = NULL()
            continue
        source = item.get("source") if item.get("source") in ("vision", "label_ocr") else "vision"
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out[field] = E(item["value"], confidence, source)
    return out


def _vision_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_VISION_PROVIDER", _PROVIDER_STACK.get("vision_provider", "openai"))
    return admin_config.get(config, "provider_stack.vision_provider", default)


def _copy_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_COPY_PROVIDER", _PROVIDER_STACK.get("copy_provider", "openai"))
    return admin_config.get(config, "provider_stack.copy_provider", default)


def _image_process_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER", _PROVIDER_STACK.get("image_process_provider", "photoroom_clipdrop"))
    return admin_config.get(config, "provider_stack.image_process_provider", default)


def _hero_generate_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_HERO_GENERATE_PROVIDER", _PROVIDER_STACK.get("hero_generate_provider", "gemini"))
    return admin_config.get(config, "provider_stack.hero_generate_provider", default)


def _truthy(value, default=True):
    """Parse a config/env flag. None -> default; otherwise accept bool or the
    usual string forms ('1'/'true'/'yes'/'on' vs '0'/'false'/'no'/'off')."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _persist_hero_prompt_enabled(config=None):
    """Whether the exact website-hero generation prompt is persisted onto the
    product record (WebsiteHeroPrompt). On by default; a customer can disable it
    via RESALE_LISTING_AI_PERSIST_HERO_PROMPT=0 or the provider_stack.hero_persist_prompt
    admin override."""
    env = os.environ.get("RESALE_LISTING_AI_PERSIST_HERO_PROMPT")
    default = _truthy(env, _truthy(_PROVIDER_STACK.get("hero_persist_prompt"), True))
    return _truthy(admin_config.get(config, "provider_stack.hero_persist_prompt", default), True)


def vision_identify(submission, ledger=None, ocr_text=None, config=None):
    """Call the configured vision provider (OpenAI Vision by default; Gemini
    or OpenRouter when RESALE_LISTING_AI_VISION_PROVIDER selects them) to extract
    Product identity fields from the submission's images (+ optional notes,
    per FR-8). Returns an envelope dict keyed by real Product schema field
    names; unsupported fields come back NULL.

    Logs its own cost row when given a ledger (optional, so a direct caller
    that doesn't care about cost still works). Real branches log a
    provider-specific service tag and that provider's real unit cost, so
    run_live_demo.py's per-provider cost comparison is honest -- Gemini's free
    tier must not report the same number as a paid provider. Stub mode keeps
    the historical flat ("vision", $0.030) row byte-identical.

    ocr_text (optional): raw text a local OCR pre-pass (adapters.ocr_read)
    read off the submission's images. When non-empty, folded into the prompt
    instructions as a distinct labeled context block -- it never fills a
    field directly; the model still owns every value/confidence/source."""
    sid = submission.get("submission_id")
    if _stub_mode():
        if ledger is not None:
            ledger.add(sid, "identify", "vision", 0.030)
        return _stub_vision_identify(submission)

    provider = _vision_provider(config)
    images = submission.get("images", [])
    notes = submission.get("notes")
    vision_instructions = _instructions_with_ocr(VISION_INSTRUCTIONS, ocr_text)
    retry_exceptions = None

    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_vision", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        instructions = _instructions_with_schema(vision_instructions, VISION_JSON_SCHEMA)
        _call = lambda: gemini.vision_json(instructions, notes, images)
    elif provider == "bedrock":
        from .providers import bedrock
        service, cost = "bedrock_vision", bedrock.BEDROCK_UNIT_COST_CAD
        retry_exceptions = _BEDROCK_RETRY_ERRORS
        instructions = _instructions_with_schema(vision_instructions, VISION_JSON_SCHEMA)
        _call = _bedrock_guard(lambda: bedrock.vision_json(instructions, notes, images))
    elif provider == "openrouter":
        from .providers import openrouter
        service, cost = "openrouter_vision", openrouter.OPENROUTER_UNIT_COST_CAD
        instructions = _instructions_with_schema(vision_instructions, VISION_JSON_SCHEMA)
        _call = lambda: openrouter.vision_json(instructions, notes, images)
    else:
        service, cost = "openai_vision", VISION_UNIT_COST_CAD

        def _call():
            client = _get_openai_client()
            content = [{"type": "input_text", "text": vision_instructions}]
            if notes:
                content.append({"type": "input_text", "text": f"Submitter notes: {notes}"})
            for image in images:
                content.append({"type": "input_image", "image_url": _image_data_url(image)})
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "product_identity",
                        "schema": VISION_JSON_SCHEMA,
                        "strict": True,
                    }
                },
            )
            return response.output_text, _responses_usage(response, VISION_MODEL)

    raw_text = _call_with_cost(ledger, sid, "identify", service, cost, _call,
                                retry_exceptions=retry_exceptions)
    raw = repair_json(raw_text, VISION_JSON_SCHEMA, "product_identity",
                       submission_id=submission.get("submission_id"), stage="identify", config=config)
    return _map_vision_output(raw)


_MEASURES_CONF = 0.75  # confidence for spec-sheet-sourced dimensions (external_lookup)

_MEASURE_FIELD_MAP = {
    "unpackaged": {
        "length_cm": "UnpackagedLengthCM", "width_cm": "UnpackagedWidthCM",
        "height_cm": "UnpackagedHeightCM", "weight_kg": "UnpackagedWeightKG",
    },
    "packaged": {
        "length_cm": "PackagedLengthCM", "width_cm": "PackagedWidthCM",
        "height_cm": "PackagedHeightCM", "weight_kg": "PackagedWeightKG",
    },
}


def _map_measures_output(raw):
    """Map the spec-lookup web-search result onto Product dimension fields. No
    match / missing figure -> field simply absent (stays NULL in blank_product),
    never fabricated. Numbers only; a non-numeric figure is dropped."""
    out = {}
    if not isinstance(raw, dict) or not raw.get("match_found"):
        return out
    for block, mapping in _MEASURE_FIELD_MAP.items():
        section = raw.get(block)
        if not isinstance(section, dict):
            continue
        for key, field in mapping.items():
            val = section.get(key)
            if val is None:
                continue
            try:
                out[field] = E(float(val), _MEASURES_CONF, "external_lookup")
            except (TypeError, ValueError):
                continue
    if out and raw.get("source_url"):
        out["ProductSpecificationsSourceURL"] = E(raw["source_url"], _MEASURES_CONF, "external_lookup")
    return out


def measures_lookup(submission, identity=None, ledger=None, config=None):
    """Physical dimensions/weight for the Product. Stub mode returns the demo
    measures keyed by hint; real mode does a manufacturer-spec-sheet web search
    (same web_search seam as retail_price) against the confirmed identity and maps
    packaged/unpackaged figures onto the dimension fields. No identity/notes to
    search on -> nothing looked up, nothing fabricated."""
    if _stub_mode():
        return dict(_KNOWN.get(submission.get("hint"), {}).get("measures", {}))

    query = _identity_query_text(identity) or (submission.get("notes") or "")
    if not query:
        return {}
    instructions = _SPEC_LOOKUP_TEMPLATE.replace("{query}", query)
    try:
        raw = _web_search_json(
            instructions, SPEC_LOOKUP_SCHEMA, "spec_lookup",
            ledger=ledger, submission_id=submission.get("submission_id"), stage="measures", config=config,
        )
    except _WEB_SEARCH_PROVIDER_ERRORS:
        # Provider failed AND exhausted every retry (each attempt already logged).
        # Dimensions are optional enrichment -> degrade to an empty map (fields
        # stay null), exactly like the no-query path, rather than propagating and
        # aborting the submission into SQS redelivery and eventually the DLQ.
        return {}
    return _map_measures_output(raw)


def _stub_unit_observe(submission):
    return dict(_KNOWN.get(submission.get("hint"), {}).get("unit", {}))


def _map_unit_output(raw):
    """Map the model's raw {field: {value, confidence, source}} JSON onto the
    {value, confidence, source} envelope, keyed by real Listing schema field
    names. Missing/blank/malformed entries -> NULL, never fabricated."""
    out = {}
    for field in UNIT_FIELDS:
        item = raw.get(field) if isinstance(raw, dict) else None
        if not isinstance(item, dict) or _is_blank_value(item.get("value")):
            out[field] = NULL()
            continue
        source = item.get("source") if item.get("source") in ("vision", "label_ocr", "context") else "vision"
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out[field] = E(item["value"], confidence, source)
    return out


def unit_observe(submission, ledger=None, config=None):
    """Call the configured vision provider (OpenAI Vision by default; Gemini
    or OpenRouter when RESALE_LISTING_AI_VISION_PROVIDER selects them) to observe THIS
    physical unit's condition/completeness/defects/testing from the
    submission's images (+ optional notes). Returns an envelope dict keyed by
    real Listing schema field names; unsupported fields come back NULL.

    Mirrors vision_identify's stub/real + provider-dispatch structure exactly,
    logging its own cost row (stage 'identify', a distinct *_vision_observe
    service tag) when given a ledger. Stub mode keeps the historical behavior
    byte-identical: no cost row at all (unlike vision_identify's flat row)."""
    sid = submission.get("submission_id")
    if _stub_mode():
        return _stub_unit_observe(submission)

    provider = _vision_provider(config)
    images = submission.get("images", [])
    notes = submission.get("notes")
    retry_exceptions = None

    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_vision_observe", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        instructions = _instructions_with_schema(UNIT_INSTRUCTIONS, UNIT_JSON_SCHEMA)
        _call = lambda: gemini.vision_json(instructions, notes, images)
    elif provider == "bedrock":
        from .providers import bedrock
        service, cost = "bedrock_vision_observe", bedrock.BEDROCK_UNIT_COST_CAD
        retry_exceptions = _BEDROCK_RETRY_ERRORS
        instructions = _instructions_with_schema(UNIT_INSTRUCTIONS, UNIT_JSON_SCHEMA)
        _call = _bedrock_guard(lambda: bedrock.vision_json(instructions, notes, images))
    elif provider == "openrouter":
        from .providers import openrouter
        service, cost = "openrouter_vision_observe", openrouter.OPENROUTER_UNIT_COST_CAD
        instructions = _instructions_with_schema(UNIT_INSTRUCTIONS, UNIT_JSON_SCHEMA)
        _call = lambda: openrouter.vision_json(instructions, notes, images)
    else:
        service, cost = "openai_vision_observe", VISION_UNIT_COST_CAD

        def _call():
            client = _get_openai_client()
            content = [{"type": "input_text", "text": UNIT_INSTRUCTIONS}]
            if notes:
                content.append({"type": "input_text", "text": f"Submitter notes: {notes}"})
            for image in images:
                content.append({"type": "input_image", "image_url": _image_data_url(image)})
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "unit_observation",
                        "schema": UNIT_JSON_SCHEMA,
                        "strict": True,
                    }
                },
            )
            return response.output_text, _responses_usage(response, VISION_MODEL)

    raw_text = _call_with_cost(ledger, sid, "identify", service, cost, _call,
                                retry_exceptions=retry_exceptions)
    raw = repair_json(raw_text, UNIT_JSON_SCHEMA, "unit_observation",
                       submission_id=submission.get("submission_id"), stage="identify", config=config)
    return _map_unit_output(raw)


def _stub_image_match(submission, identity):
    """Reverse-image search to CONFIRM identity when there is no readable UPC.
    Real: Google Lens / Google Cloud Vision Web Detection. Stub: confirms a known
    product, returns no match for unknowns."""
    if submission.get("hint") in _KNOWN:
        return {"matched": True, "confidence": 0.88, "source": "external_lookup"}
    return {"matched": False, "confidence": 0.0, "source": "none"}


def _stub_retail_price(submission, ledger=None):
    """New-market Canadian retail -> the pricing anchor (Amazon.ca + Google
    Shopping). Logs the flat pricing-stage placeholder cost here -- a single
    $0.040 row covering the whole pricing lookup (retail + resale combined),
    exactly matching the pre-fix behavior where pipeline.py logged this flat
    cost itself after calling the old pricing_lookup(). _stub_resale_price
    (below, unchanged) logs nothing itself, so stub-mode cost_ledger output
    stays byte-identical after this fix."""
    if ledger is not None:
        ledger.add(submission.get("submission_id"), "pricing", "web_search", 0.040)
    return dict(_KNOWN.get(submission.get("hint"), {}).get("pricing", {}))


def _stub_resale_price(submission, identity):
    """Used resale context from eBay.ca sold listings -> comparable fields (not the anchor)."""
    if submission.get("hint") in _KNOWN:
        return {
            "ComparableBrand": E("Acme", 0.6, "external_lookup"),
            "ComparableProductName": E("Smart Doorbell (used, sold)", 0.6, "external_lookup"),
            "ComparableProductPriceCAD": E(95.00, 0.55, "external_lookup"),
            "ComparableProductURL": E("https://www.ebay.ca/...", 0.6, "external_lookup"),
        }
    return {}


# ---- real web-search-with-citations provider (image_match/retail_price/resale_price) ----
# ONE provider backs all three seams. Selectable in
# config/provider_stack.json; override at runtime with RESALE_LISTING_AI_WEB_SEARCH_PROVIDER.
_PROVIDER_STACK = json.loads((CONFIG_DIR / "provider_stack.json").read_text())

WEB_SEARCH_MODEL = os.environ.get("RESALE_LISTING_AI_WEB_SEARCH_MODEL", "gpt-4o-mini")
PERPLEXITY_MODEL = os.environ.get("RESALE_LISTING_AI_PERPLEXITY_MODEL", "sonar")
# Nominal per-call estimate; tune from real usage.
WEB_SEARCH_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_WEB_SEARCH_UNIT_COST_CAD", "0.02"))

try:
    from openai import APIConnectionError as _APIConnectionError
    from openai import APIStatusError as _APIStatusError
    from openai import RateLimitError as _RateLimitError

    _TRANSIENT_ERRORS = (_APIConnectionError, _APIStatusError, _RateLimitError)
except ImportError:  # pragma: no cover - openai only required for real (non-stub) calls
    _TRANSIENT_ERRORS = ()

# google-genai raises its OWN exception hierarchy, unrelated to openai's, so a
# Gemini 429/5xx would otherwise escape _call_with_cost's retry/except branch
# entirely -- propagating on the first attempt with NO cost-ledger row, breaking
# the "log every call, including retries and failures" rule (project rule).
# Verified against the installed google-genai: ClientError (4xx, incl. 429 rate
# limits) and ServerError (5xx) both subclass APIError, which errors.py also
# raises directly for any other error status code -- so APIError alone covers
# every transient HTTP failure from this SDK.
# OpenRouter needs no equivalent: it reuses the openai SDK (different base_url),
# so it is already covered by _TRANSIENT_ERRORS above.
try:
    from google.genai.errors import APIError as _GeminiAPIError

    _GEMINI_TRANSIENT_ERRORS = (_GeminiAPIError,)
except ImportError:  # pragma: no cover - google-genai only required for real Gemini calls
    _GEMINI_TRANSIENT_ERRORS = ()

# Retry tuple for the Gemini branches: the SDK-specific errors above PLUS the
# openai ones, so a branch that falls back or wraps still retries correctly.
_GEMINI_RETRY_ERRORS = _TRANSIENT_ERRORS + _GEMINI_TRANSIENT_ERRORS

try:
    import requests as _requests
    # Network-shaped requests failures worth retrying; a non-retryable HTTP 4xx
    # is converted to _NonRetryableProviderError by the photoroom hero branch
    # before it reaches this set.
    _PHOTOROOM_RETRY_ERRORS = (_requests.HTTPError, _requests.ConnectionError, _requests.Timeout)
except ImportError:
    _PHOTOROOM_RETRY_ERRORS = ()

# A web-search enrichment call (image_match / retail_price / resale_price) that
# reaches its adapter boundary having ALREADY exhausted _call_with_cost's retries
# raises one of these: the openai types cover the openai web_search / Perplexity /
# OpenRouter-online paths (all on the openai SDK), the gemini types cover the
# gemini_search path. These stages are OPTIONAL pricing/confirmation enrichment,
# so a persistent provider failure (a quota-exhausted 429 RESOURCE_EXHAUSTED, a
# 5xx that never clears) must degrade to the SAME empty envelope the no-match
# path returns and let ingest finish "Accepted with unknowns" -- never propagate
# and abort an otherwise-completable submission out of _stage_ingest, leaving the
# SQS message undeleted to redeliver and eventually dead-letter.
#
# This does NOT swallow a genuinely-transient blip that could succeed on retry:
# those are handled INSIDE _call_with_cost (it retries, and only a failure that
# persists across every attempt reaches here). Nor does it mask a misconfiguration
# -- a missing API key raises RuntimeError, deliberately absent from this tuple, so
# it still fails loudly instead of silently blanking every submission. Bedrock is
# not a web-search provider, so its error types are intentionally absent too.
_WEB_SEARCH_PROVIDER_ERRORS = _TRANSIENT_ERRORS + _GEMINI_TRANSIENT_ERRORS

# botocore raises ClientError for every server-returned Bedrock failure, so it
# is the SDK exception the Bedrock branches retry on — same posture as
# _GEMINI_TRANSIENT_ERRORS above. But unlike Gemini's APIError, ClientError also
# covers failures that can NEVER succeed on retry (ValidationException from a
# bad model id / oversized image / mask mismatch, AccessDeniedException), so the
# ERROR CODE is filtered in _bedrock_guard below before the retry loop ever sees
# it — retrying those would bill three times for work that cannot work.
#
# ClientError is NOT the whole story though: a request that never reaches (or
# never completes at) the service raises a BotoCoreError subclass instead —
# read/connect timeouts (plausible now that the Nova Canvas client runs a 300s
# read timeout for long image generations), endpoint-connection failures, and
# credential-resolution failures. Those are NOT ClientErrors, so without the
# branches below they would escape _bedrock_guard AND _call_with_cost's retry
# branch entirely and propagate with NO cost row logged — breaking the project's
# "log every call, including retries and failures" rule.
try:
    from botocore.exceptions import BotoCoreError as _BotoCoreError
    from botocore.exceptions import ClientError as _BotoClientError
    from botocore.exceptions import ConnectTimeoutError as _BotoConnectTimeoutError
    from botocore.exceptions import EndpointConnectionError as _BotoEndpointConnectionError
    from botocore.exceptions import ReadTimeoutError as _BotoReadTimeoutError

    # Network-shaped BotoCoreErrors: the transport failed, so a retry can
    # plausibly succeed — same treatment as a retryable ClientError code.
    _BEDROCK_NETWORK_ERRORS = (
        _BotoReadTimeoutError, _BotoConnectTimeoutError, _BotoEndpointConnectionError,
    )
    _BEDROCK_CLIENT_ERRORS = (_BotoClientError,)
    # Every other BotoCoreError (NoCredentialsError, ParamValidationError, ...):
    # caught so a cost row is logged, but never retried.
    _BEDROCK_CORE_ERRORS = (_BotoCoreError,)
except ImportError:  # pragma: no cover - boto3 only required for real (non-stub) calls
    _BEDROCK_NETWORK_ERRORS = ()
    _BEDROCK_CLIENT_ERRORS = ()
    _BEDROCK_CORE_ERRORS = ()

# What _call_with_cost's retry branch must catch for a Bedrock call: the
# retryable ClientError codes and the network-shaped BotoCoreErrors both reach
# it unwrapped from _bedrock_guard.
_BEDROCK_TRANSIENT_ERRORS = _BEDROCK_CLIENT_ERRORS + _BEDROCK_NETWORK_ERRORS

_BEDROCK_RETRY_ERRORS = _TRANSIENT_ERRORS + _BEDROCK_TRANSIENT_ERRORS

# Response-parsing failures raised by resale_listing_ai/providers/bedrock.py's own code
# (e.g. _invoke_image_model's "response contained no image" ValueError, or a
# KeyError/IndexError from a malformed Converse response). The call WAS made and
# billed, so it must be logged — but no retry can repair a response the provider
# already returned in that shape.
_BEDROCK_RESPONSE_ERRORS = (ValueError, KeyError, IndexError)

# The only Bedrock error codes where a retry can plausibly succeed.
_BEDROCK_RETRYABLE_CODES = frozenset({
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "TooManyRequestsException",
    "InternalServerException",
})


class _NonRetryableProviderError(Exception):
    """Marker telling _call_with_cost "this attempt failed and no retry can fix
    it". _call_with_cost still logs the attempt's cost row (the call WAS made —
    the project's log-every-call rule), then re-raises the provider's own original
    exception so callers see the real error, not this wrapper."""

    def __init__(self, original):
        super().__init__(str(original))
        self.original = original


def _bedrock_guard(fn):
    """Wrap a Bedrock call so only genuinely retryable failures reach
    _call_with_cost's retry path; everything else fails fast after ONE logged
    attempt (never zero — an unlogged failure would hide a real, billed call).

    Retryable, re-raised unwrapped so _call_with_cost's `except retry_exceptions`
    branch retries them: the throttling/transient ClientError codes, and the
    network-shaped BotoCoreErrors (read/connect timeout, endpoint connection).

    Not retryable, wrapped in _NonRetryableProviderError so _call_with_cost logs
    exactly one cost row and re-raises the ORIGINAL exception: every other
    ClientError code (ValidationException, AccessDeniedException, ...), every
    other BotoCoreError (NoCredentialsError, ...), and the provider module's own
    response-parsing errors."""
    def _wrapped():
        try:
            return fn()
        except _BEDROCK_NETWORK_ERRORS:
            # Transport failed -> a retry can plausibly succeed. Must be listed
            # before _BEDROCK_CORE_ERRORS: these are BotoCoreError subclasses.
            raise
        except _BEDROCK_CLIENT_ERRORS as exc:
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            if code not in _BEDROCK_RETRYABLE_CODES:
                raise _NonRetryableProviderError(exc) from exc
            raise
        except _BEDROCK_CORE_ERRORS as exc:
            raise _NonRetryableProviderError(exc) from exc
        except _BEDROCK_RESPONSE_ERRORS as exc:
            raise _NonRetryableProviderError(exc) from exc

    return _wrapped


def _instructions_with_schema(instructions, schema):
    """Append the target JSON schema to a prompt-instruction string.

    The OpenAI (and Perplexity) paths convey the target field shape STRUCTURALLY,
    via the API's own json_schema response format. Gemini and OpenRouter are
    called here with a loose JSON response mode instead (Gemini's google_search
    tool cannot be combined with structured output in the same call -- see
    resale_listing_ai/providers/gemini.py), so the shape has to travel in the prompt
    text or the model never learns the real field names. That matters because
    json_repair._coerce_to_schema projects onto our schema by EXACT key lookup:
    a model guessing its own key names would null out every field, and a real
    Gemini/OpenRouter run would produce an all-NULL identity and be rejected by
    the gate."""
    return instructions + "\n\nReturn JSON matching exactly this schema:\n" + json.dumps(schema)


def _web_search_provider(config=None):
    default = os.environ.get(
        "RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", _PROVIDER_STACK.get("web_search_provider", "openai_web_search")
    )
    return admin_config.get(config, "provider_stack.web_search_provider", default)


def _identity_query_text(identity):
    """Confirmed identity, as text, for a pricing/confirmation search query.
    PRD 11.2: confirm exact brand/model/variant/colour before comparing prices."""
    if not isinstance(identity, dict):
        return ""
    parts = []
    for field in ("Brand", "ProductName", "ModelNumber", "Variant", "Colour"):
        env = identity.get(field)
        if isinstance(env, dict) and env.get("value"):
            parts.append(str(env["value"]))
    return " ".join(parts)


def _call_with_cost(ledger, submission_id, stage, service, unit_cost_cad, fn,
                     retry_exceptions=None, max_retries=2, sleep=None):
    """Call fn(), logging one cost-ledger row per attempt — including retries — so a
    flaky call is visible in the per-listing cost, never hidden (System Design §9)."""
    import time as _time

    retry_exceptions = _TRANSIENT_ERRORS if retry_exceptions is None else retry_exceptions
    sleep = _time.sleep if sleep is None else sleep
    last_exc = None
    for attempt in range(max_retries + 1):
        suffix = f" (retry {attempt})" if attempt > 0 else ""
        print(f"  [{stage}] {service}: calling...{suffix}", file=sys.stderr, flush=True)
        start = _time.monotonic()
        try:
            result = fn()
        except _NonRetryableProviderError as exc:
            # Attempt made -> cost row logged, but the failure is permanent, so
            # stop here instead of billing two more doomed attempts.
            if ledger is not None:
                ledger.add(submission_id, stage, service, unit_cost_cad, retry=(attempt > 0))
            print(f"  [{stage}] {service}: failed ({_time.monotonic() - start:.1f}s) -- {exc.original}",
                  file=sys.stderr, flush=True)
            raise exc.original
        except retry_exceptions as exc:
            last_exc = exc
            if ledger is not None:
                ledger.add(submission_id, stage, service, unit_cost_cad, retry=(attempt > 0))
            print(f"  [{stage}] {service}: error ({_time.monotonic() - start:.1f}s) -- {exc}",
                  file=sys.stderr, flush=True)
            if attempt < max_retries:
                sleep(1.0 * (2 ** attempt))
            continue
        else:
            text, usage = _split_call_result(result)
            cost, tokens = _cost_from_usage(unit_cost_cad, usage)
            if ledger is not None:
                ledger.add(submission_id, stage, service, cost, retry=(attempt > 0), **tokens)
            print(f"  [{stage}] {service}: done ({_time.monotonic() - start:.1f}s)", file=sys.stderr, flush=True)
            return text
    raise last_exc


def _responses_usage(response, model):
    """Token usage from an OpenAI Responses API result as the {model,
    input_tokens, output_tokens} dict _call_with_cost prices from, or None when
    the response carries no usage (e.g. a test double) so cost falls back to flat."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {"model": model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None)}


def _split_call_result(result):
    """A billed fn() returns either bare text (legacy -> flat cost) or a
    (text, usage) pair, where usage is {model, input_tokens, output_tokens} or
    None. Returns (text, usage-or-None)."""
    if (isinstance(result, tuple) and len(result) == 2
            and (result[1] is None or isinstance(result[1], dict))):
        return result[0], result[1]
    return result, None


def _cost_from_usage(unit_cost_cad, usage):
    """Resolve the ledger cost + token columns for one successful call.
    Token-based cost when usage is present and the model is in the rate table;
    otherwise the flat unit_cost_cad. Token counts + model are recorded whenever
    usage is present, even on the flat fallback, so cost stays auditable."""
    if not usage:
        return unit_cost_cad, {}
    tokens = {"input_tokens": usage.get("input_tokens"),
              "output_tokens": usage.get("output_tokens"),
              "model": usage.get("model")}
    token_cost = rates.token_cost_cad(
        usage.get("model"), usage.get("input_tokens"), usage.get("output_tokens"))
    return (token_cost if token_cost is not None else unit_cost_cad), tokens


def _get_perplexity_client():
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "PERPLEXITY_API_KEY not set (web_search_provider=perplexity_sonar). Set it in the "
            "environment, switch RESALE_LISTING_AI_WEB_SEARCH_PROVIDER back to openai_web_search, or set "
            "RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    from openai import OpenAI  # Perplexity's API is OpenAI-chat-completions-compatible

    return OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")


def _openai_web_search_json(instructions, schema, schema_name):
    client = _get_openai_client()
    response = client.responses.create(
        model=WEB_SEARCH_MODEL,
        tools=[{"type": "web_search"}],
        input=[{"role": "user", "content": [{"type": "input_text", "text": instructions}]}],
        text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
    )
    return response.output_text, _responses_usage(response, WEB_SEARCH_MODEL)


def _perplexity_json(instructions, schema, schema_name):
    client = _get_perplexity_client()
    response = client.chat.completions.create(
        model=PERPLEXITY_MODEL,
        messages=[{"role": "user", "content": instructions}],
        response_format={"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
    )
    return response.choices[0].message.content


def _web_search_json(instructions, schema, schema_name, *, ledger, submission_id, stage, config=None):
    provider = _web_search_provider(config)
    retry_exceptions = None
    if provider == "perplexity_sonar":
        service, cost = "perplexity_sonar", WEB_SEARCH_UNIT_COST_CAD
        fn = lambda: _perplexity_json(instructions, schema, schema_name)
    elif provider == "gemini_search":
        from .providers import gemini
        service, cost = "gemini_search", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        grounded_instructions = _instructions_with_schema(instructions, schema)
        fn = lambda: gemini.web_search_json(grounded_instructions)
    elif provider == "openrouter_online":
        from .providers import openrouter
        service, cost = "openrouter_online", openrouter.OPENROUTER_UNIT_COST_CAD
        grounded_instructions = _instructions_with_schema(instructions, schema)
        fn = lambda: openrouter.web_search_json(grounded_instructions)
    else:
        service, cost = "openai_web_search", WEB_SEARCH_UNIT_COST_CAD
        fn = lambda: _openai_web_search_json(instructions, schema, schema_name)
    raw_text = _call_with_cost(ledger, submission_id, stage, service, cost, fn,
                                retry_exceptions=retry_exceptions)
    return repair_json(raw_text, schema, schema_name, ledger=ledger, submission_id=submission_id, stage=stage,
                        config=config)


_IMAGE_MATCH_PROMPT = _load_prompt("image_match")
IMAGE_MATCH_SCHEMA = _IMAGE_MATCH_PROMPT["json_schema"]
_validate_strict_schema(IMAGE_MATCH_SCHEMA, "image_match")
_IMAGE_MATCH_TEMPLATE = _IMAGE_MATCH_PROMPT["instructions"]

_RETAIL_PRICE_PROMPT = _load_prompt("retail_price")
RETAIL_PRICE_SCHEMA = _RETAIL_PRICE_PROMPT["json_schema"]
_validate_strict_schema(RETAIL_PRICE_SCHEMA, "retail_price")
_RETAIL_PRICE_TEMPLATE = _RETAIL_PRICE_PROMPT["instructions"]

_RESALE_PRICE_PROMPT = _load_prompt("resale_price")
RESALE_PRICE_SCHEMA = _RESALE_PRICE_PROMPT["json_schema"]
_validate_strict_schema(RESALE_PRICE_SCHEMA, "resale_price")
_RESALE_PRICE_TEMPLATE = _RESALE_PRICE_PROMPT["instructions"]

_SPEC_LOOKUP_PROMPT = _load_prompt("spec_lookup")
SPEC_LOOKUP_SCHEMA = _SPEC_LOOKUP_PROMPT["json_schema"]
_validate_strict_schema(SPEC_LOOKUP_SCHEMA, "spec_lookup")
_SPEC_LOOKUP_TEMPLATE = _SPEC_LOOKUP_PROMPT["instructions"]


def _map_image_match_output(raw):
    if not isinstance(raw, dict) or not raw.get("matched"):
        return {"matched": False, "confidence": 0.0, "source": "none"}
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"matched": True, "confidence": confidence, "source": "external_lookup"}


def _map_retail_price_output(raw, today_iso):
    """Never fabricated: no match -> every pricing field NULL (LastPricingCheckDate still
    records that a check was attempted). CAD conversion is a prompt constraint on the model
    (PROVIDER_STACK.md) — price_cad is trusted as already-converted CAD; original_currency
    is retained only as an audit note, not recomputed here."""
    empty = {
        "PricingAnchorPriceCAD": NULL(), "PricingAnchorSourceName": NULL(), "PricingAnchorSourceURL": NULL(),
        "CorroboratingSourcePriceCAD": NULL(), "CorroboratingSourceName": NULL(), "CorroboratingSourceURL": NULL(),
        "PricingConfidence": NULL(), "LastPricingCheckDate": E(today_iso, 1.0, "derived"),
    }
    if not isinstance(raw, dict) or not raw.get("match_found") or not isinstance(raw.get("anchor"), dict):
        return empty

    anchor = raw["anchor"]
    try:
        confidence = float(anchor.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    out = dict(empty)
    out["PricingAnchorPriceCAD"] = E(float(anchor["price_cad"]), confidence, "external_lookup")
    out["PricingAnchorSourceName"] = E(anchor["source_name"], confidence, "external_lookup")
    out["PricingAnchorSourceURL"] = E(anchor["source_url"], confidence, "external_lookup")
    out["PricingConfidence"] = E(raw.get("confidence_label", "Low"), confidence, "derived")

    corroborating = raw.get("corroborating")
    if isinstance(corroborating, dict) and corroborating.get("price_cad") is not None:
        out["CorroboratingSourcePriceCAD"] = E(float(corroborating["price_cad"]), confidence, "external_lookup")
        out["CorroboratingSourceName"] = E(corroborating.get("source_name"), confidence, "external_lookup")
        out["CorroboratingSourceURL"] = E(corroborating.get("source_url"), confidence, "external_lookup")
    return out


def _map_resale_price_output(raw):
    if not isinstance(raw, dict) or not raw.get("match_found") or not isinstance(raw.get("comparable"), dict):
        return {}
    comparable = raw["comparable"]
    try:
        confidence = float(comparable.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "ComparableBrand": E(comparable["brand"], confidence, "external_lookup"),
        "ComparableProductName": E(comparable["product_name"], confidence, "external_lookup"),
        "ComparableProductPriceCAD": E(float(comparable["price_cad"]), confidence, "external_lookup"),
        "ComparableProductURL": E(comparable["source_url"], confidence, "external_lookup"),
    }


def image_match(submission, identity, ledger=None, config=None):
    """Confirm product identity via web search when there is no confident UPC (the
    DEFAULT provider sidesteps gated reverse-image APIs — see PROVIDER_STACK.md). No
    identity/notes to confirm -> no match, no API call, nothing fabricated."""
    if _stub_mode():
        return _stub_image_match(submission, identity)

    query = _identity_query_text(identity) or (submission.get("notes") or "")
    if not query:
        return {"matched": False, "confidence": 0.0, "source": "none"}

    instructions = _IMAGE_MATCH_TEMPLATE.replace("{query}", query)
    try:
        raw = _web_search_json(
            instructions, IMAGE_MATCH_SCHEMA, "image_match",
            ledger=ledger, submission_id=submission.get("submission_id"), stage="identify", config=config,
        )
    except _WEB_SEARCH_PROVIDER_ERRORS:
        # Provider failed AND exhausted every retry (each attempt already logged).
        # Confirmation is optional enrichment -> degrade to no-match, exactly like
        # the empty-query path above, rather than propagating and aborting ingest.
        return {"matched": False, "confidence": 0.0, "source": "none"}
    return _map_image_match_output(raw)


def retail_price(submission, identity, ledger=None, config=None):
    """NEW-market Canadian retail price -> the pricing anchor (PricingAnchorPriceCAD +
    source), with a corroborating source when available. No identity/notes to price
    against -> every field NULL, no API call, nothing fabricated."""
    if _stub_mode():
        return _stub_retail_price(submission, ledger=ledger)

    query = _identity_query_text(identity) or (submission.get("notes") or "")
    today_iso = _today_iso()
    if not query:
        return _map_retail_price_output(None, today_iso)

    instructions = _RETAIL_PRICE_TEMPLATE.replace("{query}", query)
    try:
        raw = _web_search_json(
            instructions, RETAIL_PRICE_SCHEMA, "retail_price_cad",
            ledger=ledger, submission_id=submission.get("submission_id"), stage="pricing", config=config,
        )
    except _WEB_SEARCH_PROVIDER_ERRORS:
        # Provider failed AND exhausted every retry (each attempt already logged).
        # Pricing is optional enrichment -> degrade to the same all-NULL map the
        # no-match path returns (LastPricingCheckDate still records the attempt) so
        # ingest can finish "Accepted with unknowns", rather than propagating and
        # aborting the submission into SQS redelivery and eventually the DLQ.
        return _map_retail_price_output(None, today_iso)
    return _map_retail_price_output(raw, today_iso)


def resale_price(submission, identity, ledger=None, config=None):
    """Used resale context (eBay.ca-style sold/listed data) -> the Comparable* fields
    ONLY — this is context for a later internal pricing model, never the anchor. No
    identity/notes -> empty, no API call, nothing fabricated."""
    if _stub_mode():
        return _stub_resale_price(submission, identity)

    query = _identity_query_text(identity) or (submission.get("notes") or "")
    if not query:
        return {}

    instructions = _RESALE_PRICE_TEMPLATE.replace("{query}", query)
    try:
        raw = _web_search_json(
            instructions, RESALE_PRICE_SCHEMA, "resale_price_cad",
            ledger=ledger, submission_id=submission.get("submission_id"), stage="pricing", config=config,
        )
    except _WEB_SEARCH_PROVIDER_ERRORS:
        # Provider failed AND exhausted every retry (each attempt already logged).
        # Comparable* resale context is optional enrichment -> degrade to an empty
        # map (no comparable fields), exactly like the no-match path, rather than
        # propagating and aborting ingest.
        return {}
    return _map_resale_price_output(raw)


def _today_iso():
    from datetime import date

    return date.today().isoformat()


# ---- image_process — classification, availability flags, selection, cleanup mask ----
_IMAGE_CLASSIFY_PROMPT = _load_prompt("image_classify")
IMAGE_CATEGORIES = tuple(_IMAGE_CLASSIFY_PROMPT["json_schema"]["properties"]["category"]["enum"])

# Listing schema field each classification category feeds (config/listing_schema.json).
_FLAG_BY_CATEGORY = {
    "actual_product": "ActualProductPhotosAvailable",
    "original_packaging": "OriginalPackagingPhotosAvailable",
    "product_information_label": "ProductInformationLabelPhotosAvailable",
    "contextual": "ContextualProductPhotosAvailable",
}

S3_BUCKET = os.environ.get("RESALE_LISTING_AI_S3_BUCKET", "resale-listing-ai-dev")


def _map_classify_output(raw):
    """Map the classification model's raw {category, needs_cleanup, clutter_bbox}
    onto a trusted shape. Malformed/unknown input -> safe defaults (contextual,
    no cleanup, no bbox) — never guessed toward a more consequential category."""
    if not isinstance(raw, dict):
        return {"category": "contextual", "needs_cleanup": False, "clutter_bbox": None}
    category = raw.get("category")
    if category not in IMAGE_CATEGORIES:
        category = "contextual"
    bbox = raw.get("clutter_bbox")
    return {
        "category": category,
        "needs_cleanup": bool(raw.get("needs_cleanup")),
        "clutter_bbox": bbox if isinstance(bbox, dict) else None,
    }


def _photo_availability_flags(classified):
    """classified: [(path, {category, needs_cleanup}), ...] -> the four Listing
    photo-availability flags, Yes iff at least one image of that category is present."""
    present = {info.get("category") for _, info in classified}
    return {
        flag: E("Yes" if category in present else "No", 1.0, "derived")
        for category, flag in _FLAG_BY_CATEGORY.items()
    }


def _select_finished_images(classified):
    """Order images for the finished set: index 0 -> hero candidate, 1..3 ->
    supporting (schema caps supporting at 3 — 'reserved capacity' beyond that).
    Prefers images classified as the actual physical unit; falls back to
    whatever was submitted when nothing was classified that way."""
    preferred = [path for path, info in classified if info.get("category") == "actual_product"]
    pool = preferred or [path for path, _ in classified]
    return pool[:4]


def _validate_bbox(bbox):
    """Validate a classifier-reported clutter bbox is well-formed normalized (0..1)
    fractions. Returns (x, y, w, h) floats, or None for missing/malformed input —
    validated BEFORE any image bytes are touched, so both cleanup-region providers
    (Clipdrop's fill mask, Gemini's drawn marker) reject the same bad input the same
    way without ever opening a possibly-invalid image for a bbox that's already known
    to be unusable."""
    if not isinstance(bbox, dict):
        return None
    try:
        x, y, w, h = (float(bbox[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 and y + h <= 1):
        return None
    return x, y, w, h


def _bbox_px(x, y, w, h, width, height):
    """Convert validated normalized-fraction bbox coords to a pixel rectangle for an
    image of the given size, clamped so it can never overshoot the image bounds."""
    left, top = int(x * width), int(y * height)
    right, bottom = min(width, left + int(w * width)), min(height, top + int(h * height))
    return left, top, right, bottom


def _build_cleanup_mask(image_bytes, bbox):
    """Build a mask covering ONLY the reported clutter bounding box, so inpainting
    can never reach outside it — real defect evidence elsewhere in the frame is
    architecturally untouched. No usable bbox -> None (skip cleanup; a background-
    removed-only image is always safe, a guessed mask is not)."""
    fractions = _validate_bbox(bbox)
    if fractions is None:
        return None

    from io import BytesIO

    from PIL import Image as PILImage
    from PIL import ImageDraw

    img = PILImage.open(BytesIO(image_bytes))
    width, height = img.size
    left, top, right, bottom = _bbox_px(*fractions, width, height)
    mask = PILImage.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle([left, top, right, bottom], fill=255)
    buf = BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def _draw_bbox_marker(image_bytes, bbox):
    """Draw a visible outline rectangle onto a copy of the image at the reported
    clutter bbox — Gemini's image-editing calls are prompt-driven, not literally
    mask-constrained like Clipdrop's, so a drawn marker plus an explicit "only edit
    inside the marked rectangle" instruction is the closest equivalent safety
    boundary: the model is told, visually, exactly which pixels it may touch. No
    usable bbox -> None (skip cleanup; same rule _build_cleanup_mask enforces)."""
    fractions = _validate_bbox(bbox)
    if fractions is None:
        return None

    from io import BytesIO

    from PIL import Image as PILImage
    from PIL import ImageDraw

    img = PILImage.open(BytesIO(image_bytes)).convert("RGB")
    width, height = img.size
    left, top, right, bottom = _bbox_px(*fractions, width, height)
    marked = img.copy()
    line_width = max(2, min(width, height) // 200)
    ImageDraw.Draw(marked).rectangle([left, top, right, bottom], outline=(255, 0, 0), width=line_width)
    buf = BytesIO()
    marked.save(buf, format="PNG")
    return buf.getvalue()


def _normalize_hero_bytes(image_bytes):
    """Force a generated hero into the required output envelope (AI-Generated
    Hero Standard section 2): composite onto a pure-white 2048x2048 canvas,
    centred, sRGB, high-quality JPEG. Deterministic, so the envelope holds
    regardless of what size/format/background the generation model returned."""
    from io import BytesIO

    from PIL import Image as PILImage

    src = PILImage.open(BytesIO(image_bytes)).convert("RGBA")
    src.thumbnail((2048, 2048), PILImage.LANCZOS)
    canvas = PILImage.new("RGBA", (2048, 2048), (255, 255, 255, 255))
    offset = ((2048 - src.width) // 2, (2048 - src.height) // 2)
    canvas.paste(src, offset, src)
    rgb = canvas.convert("RGB")  # flatten alpha onto white, sRGB
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


_IMAGE_CLASSIFY_SCHEMA = _IMAGE_CLASSIFY_PROMPT["json_schema"]
_validate_strict_schema(_IMAGE_CLASSIFY_SCHEMA, "image_classify")

IMAGE_CLASSIFY_INSTRUCTIONS = _IMAGE_CLASSIFY_PROMPT["instructions"]

IMAGE_CLASSIFY_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_IMAGE_CLASSIFY_UNIT_COST_CAD", "0.01"))
PHOTOROOM_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_PHOTOROOM_UNIT_COST_CAD", "0.02"))
CLIPDROP_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_CLIPDROP_UNIT_COST_CAD", "0.05"))

HERO_QC_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_HERO_QC_UNIT_COST_CAD", "0.01"))
OPENAI_HERO_GENERATE_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_OPENAI_HERO_GENERATE_UNIT_COST_CAD", "0.04"))
PHOTOROOM_HERO_GENERATE_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_PHOTOROOM_HERO_GENERATE_UNIT_COST_CAD", "0.05"))

_HERO_GENERATE_PROMPT = _load_prompt("hero_generate")
_HERO_GENERATE_TEMPLATE = _HERO_GENERATE_PROMPT["instructions"]

_HERO_QC_PROMPT = _load_prompt("hero_qc")
HERO_QC_SCHEMA = _HERO_QC_PROMPT["json_schema"]
_validate_strict_schema(HERO_QC_SCHEMA, "hero_qc")
_HERO_QC_TEMPLATE = _HERO_QC_PROMPT["instructions"]


def _classify_image(image_path, *, ledger, submission_id, config=None):
    """One vision call per submission image: which photo-availability category
    it is, and whether it needs tape/ruler/clutter cleanup (+ where). This IS
    "the vision step" that flags cleanup candidates — kept local to
    image_process so vision_identify's Product-identity-only contract never
    changes. Shares _vision_provider() with vision_identify: same kind of
    call (single image in, structured JSON out)."""
    provider = _vision_provider(config)
    retry_exceptions = None

    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_vision_classify", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        instructions = _instructions_with_schema(
            IMAGE_CLASSIFY_INSTRUCTIONS, _IMAGE_CLASSIFY_SCHEMA)
        _call = lambda: gemini.classify_json(instructions, image_path)
    elif provider == "bedrock":
        from .providers import bedrock
        service, cost = "bedrock_vision_classify", bedrock.BEDROCK_UNIT_COST_CAD
        retry_exceptions = _BEDROCK_RETRY_ERRORS
        instructions = _instructions_with_schema(
            IMAGE_CLASSIFY_INSTRUCTIONS, _IMAGE_CLASSIFY_SCHEMA)
        _call = _bedrock_guard(lambda: bedrock.classify_json(instructions, image_path))
    elif provider == "openrouter":
        from .providers import openrouter
        service, cost = "openrouter_vision_classify", openrouter.OPENROUTER_UNIT_COST_CAD
        instructions = _instructions_with_schema(
            IMAGE_CLASSIFY_INSTRUCTIONS, _IMAGE_CLASSIFY_SCHEMA)
        _call = lambda: openrouter.classify_json(instructions, image_path)
    else:
        service, cost = "openai_vision_classify", IMAGE_CLASSIFY_UNIT_COST_CAD

        def _call():
            client = _get_openai_client()
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": IMAGE_CLASSIFY_INSTRUCTIONS},
                    {"type": "input_image", "image_url": _image_data_url(image_path)},
                ]}],
                text={"format": {"type": "json_schema", "name": "image_classification",
                                  "schema": _IMAGE_CLASSIFY_SCHEMA, "strict": True}},
            )
            return response.output_text, _responses_usage(response, VISION_MODEL)

    raw_text = _call_with_cost(ledger, submission_id, "images", service, cost, _call,
                                retry_exceptions=retry_exceptions)
    raw = repair_json(raw_text, _IMAGE_CLASSIFY_SCHEMA, "image_classification",
                       ledger=ledger, submission_id=submission_id, stage="images", config=config)
    return _map_classify_output(raw)


_HERO_QC_KEYS = ("branding_clear", "coherent", "matches_confirmed", "no_invented")


def _map_hero_qc_output(raw):
    """Malformed model output -> None (treated by the caller as a failed check,
    never a silent pass). Booleans are coerced; missing reasons -> []."""
    if not isinstance(raw, dict):
        return None
    out = {k: bool(raw.get(k)) for k in _HERO_QC_KEYS}
    reasons = raw.get("reasons")
    out["reasons"] = [str(r) for r in reasons] if isinstance(reasons, list) else []
    return out


def _hero_qc(image_path, query, *, ledger, submission_id, config=None):
    """Vision-verify a GENERATED hero against the AI-Generated Hero Standard
    (sections 6-7): unbranded, coherent, matches the confirmed product, no
    invented features. Mirrors _classify_image's dispatch shape (single image in,
    structured JSON out). Returns the mapped QC dict or None on malformed output."""
    provider = _vision_provider(config)
    retry_exceptions = None
    instructions = _HERO_QC_TEMPLATE.replace("{query}", query)

    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_hero_qc", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        grounded = _instructions_with_schema(instructions, HERO_QC_SCHEMA)
        _call = lambda: gemini.classify_json(grounded, image_path)
    elif provider == "bedrock":
        from .providers import bedrock
        service, cost = "bedrock_hero_qc", bedrock.BEDROCK_UNIT_COST_CAD
        retry_exceptions = _BEDROCK_RETRY_ERRORS
        grounded = _instructions_with_schema(instructions, HERO_QC_SCHEMA)
        _call = _bedrock_guard(lambda: bedrock.classify_json(grounded, image_path))
    elif provider == "openrouter":
        from .providers import openrouter
        service, cost = "openrouter_hero_qc", openrouter.OPENROUTER_UNIT_COST_CAD
        grounded = _instructions_with_schema(instructions, HERO_QC_SCHEMA)
        _call = lambda: openrouter.classify_json(grounded, image_path)
    else:
        service, cost = "openai_hero_qc", HERO_QC_UNIT_COST_CAD

        def _call():
            client = _get_openai_client()
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_image", "image_url": _image_data_url(image_path)},
                ]}],
                text={"format": {"type": "json_schema", "name": "hero_qc",
                                  "schema": HERO_QC_SCHEMA, "strict": True}},
            )
            return response.output_text, _responses_usage(response, VISION_MODEL)

    raw_text = _call_with_cost(ledger, submission_id, "images", service, cost, _call,
                                retry_exceptions=retry_exceptions)
    raw = repair_json(raw_text, HERO_QC_SCHEMA, "hero_qc",
                       ledger=ledger, submission_id=submission_id, stage="images", config=config)
    return _map_hero_qc_output(raw)


# What to DRAW (unbranded visual descriptors) vs. IDENTIFICATION context that is
# given for accuracy but must never be rendered. Brand/ModelNumber drive a
# diffusion model toward trade dress even under a "do not render" instruction,
# so they are kept out of the depiction subject and fenced separately.
_HERO_DEPICTION_FIELDS = ("ProductName", "Category", "Colour", "Variant")
_HERO_IDENTIFICATION_FIELDS = ("Brand", "ModelNumber")


def _hero_confirmed_fields(identity, fields):
    if not isinstance(identity, dict):
        return ""
    parts = []
    for field in fields:
        env = identity.get(field)
        if isinstance(env, dict) and confident(env):
            parts.append(f"{field}: {env['value']}")
    return "; ".join(parts)


def _hero_depiction_text(identity):
    """Confirmed visual descriptors to draw (product type/form/colour/variant) —
    no brand or model names, so the generated product stays unbranded."""
    return _hero_confirmed_fields(identity, _HERO_DEPICTION_FIELDS)


def _hero_identification_text(identity):
    """Confirmed brand + model number, supplied to the prompt for accuracy ONLY
    and explicitly fenced so they are never rendered in the image."""
    return _hero_confirmed_fields(identity, _HERO_IDENTIFICATION_FIELDS)


def _generate_hero_bytes(instructions, reference_bytes, *, ledger, submission_id, config=None):
    """Dispatch one generation call through the hero_generate provider seam,
    cost-logged. Only the Gemini path is wired today."""
    provider = _hero_generate_provider(config)
    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_hero_generate", gemini.GEMINI_IMAGE_UNIT_COST_CAD
        _call = lambda: gemini.generate_hero_image(instructions, reference_bytes)
        return _call_with_cost(ledger, submission_id, "images", service, cost, _call,
                                retry_exceptions=_GEMINI_RETRY_ERRORS)
    if provider == "openai":
        from .providers import openai_image
        service, cost = "openai_hero_generate", OPENAI_HERO_GENERATE_UNIT_COST_CAD

        def _call():
            try:
                return openai_image.generate_hero_image(instructions, reference_bytes)
            except Exception as exc:
                # Auth/permission errors (401 missing image scope, 403 org not
                # verified) can never be fixed by retrying -- fail fast after one
                # logged attempt. Genuinely transient errors (429 rate limit,
                # connection, 5xx) propagate to _call_with_cost's retry path.
                if getattr(exc, "status_code", None) in (401, 403):
                    raise _NonRetryableProviderError(exc) from exc
                raise

        return _call_with_cost(ledger, submission_id, "images", service, cost, _call,
                                retry_exceptions=_TRANSIENT_ERRORS)
    if provider == "photoroom":
        import requests

        from .providers import photoroom_image
        service, cost = "photoroom_hero_generate", PHOTOROOM_HERO_GENERATE_UNIT_COST_CAD

        def _call():
            try:
                return photoroom_image.generate_hero_image(instructions, reference_bytes)
            except requests.HTTPError as exc:
                # 429 rate-limit and 5xx are transient -> retry; every other 4xx
                # (bad request, entitlement) can't be fixed by retrying -> fail
                # fast after one logged attempt into the NULL+review-flag path.
                status = getattr(exc.response, "status_code", None)
                if status in (429, 500, 502, 503, 504):
                    raise
                raise _NonRetryableProviderError(exc) from exc

        return _call_with_cost(ledger, submission_id, "images", service, cost, _call,
                                retry_exceptions=_PHOTOROOM_RETRY_ERRORS)
    raise _NonRetryableProviderError(f"hero_generate provider not wired: {provider}")


def _generate_website_hero(identity, product_key, storage, reference_bytes,
                            *, ledger=None, submission_id=None, config=None):
    """Generate the AI website hero (text-first), QC-gate it, retry once with the
    scrubbed reference on an accuracy-only failure, and flag for manual review
    on branding failure or persistent failure. Returns
    (envelope, review_required, prompt, rejected_url) -- the exact generation
    prompt is returned (even on rejection/failure) so it can be persisted for
    audit/tuning (it is "" only when no confident identity was available to build
    one); rejected_url is the storage URL of the QC-rejected image, saved for the
    admin review link, and "" whenever nothing was rejected/saved.
    Never raises, never substitutes a branded or actual-item image."""
    query = _identity_query_text(identity)
    if not query:
        return NULL(), True, "", ""
    depiction = _hero_depiction_text(identity) or query
    identification = _hero_identification_text(identity)
    instructions = (_HERO_GENERATE_TEMPLATE
                    .replace("{depiction}", depiction)
                    .replace("{identification}", identification))

    import tempfile

    def _attempt(reference):
        raw = _generate_hero_bytes(instructions, reference,
                                    ledger=ledger, submission_id=submission_id, config=config)
        normalized = _normalize_hero_bytes(raw)
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            tmp.write(normalized)
            tmp.flush()
            qc = _hero_qc(tmp.name, query, ledger=ledger, submission_id=submission_id, config=config)
        return normalized, qc

    def _passed(qc):
        return qc is not None and all(qc[k] for k in _HERO_QC_KEYS)

    def _accuracy_only(qc):
        # branding is clean but some accuracy check failed -> a reference retry may help
        return qc is not None and qc["branding_clear"] and not _passed(qc)

    def _log_qc_rejection(qc, rejected_bytes):
        """Surface WHY the generated hero was rejected (which checks failed and the
        model's reasons), and save the rejected image for manual inspection so the
        prompt/QC can be tuned. Returns the saved image's storage URL for the admin
        review link, or "" if logging/saving failed. Best-effort: never let
        logging/saving break the safe-degrade path."""
        try:
            if qc is None:
                print(f"[hero] QC rejected {product_key}: malformed QC response", file=sys.stderr, flush=True)
            else:
                failed = [k for k in _HERO_QC_KEYS if not qc[k]]
                print(f"[hero] QC rejected {product_key}: failed={failed} reasons={qc.get('reasons', [])}",
                      file=sys.stderr, flush=True)
            return _put_object(storage, f"finished/{product_key}/website_hero_rejected.jpg",
                               rejected_bytes, content_type="image/jpeg")
        except Exception:
            return ""

    # Photoroom's hero path EDITS the real product photo (editWithAI), so it needs
    # the reference on the first attempt -- a text-only pass would regenerate a
    # generic guess. Text-first providers (gemini/openai) still lead with None and
    # only fall back to the scrubbed reference on an accuracy-only QC failure.
    first_reference = reference_bytes if _hero_generate_provider(config) == "photoroom" else None
    try:
        normalized, qc = _attempt(first_reference)
        if not _passed(qc) and _accuracy_only(qc) and first_reference is None:
            normalized, qc = _attempt(reference_bytes)
        if not _passed(qc):
            rejected_url = _log_qc_rejection(qc, normalized)
            return NULL(), True, instructions, rejected_url or ""
        url = _put_object(storage, f"finished/{product_key}/website_hero.jpg",
                          normalized, content_type="image/jpeg")
        return E(url, 0.86, "generated"), False, instructions, ""
    except Exception:
        return NULL(), True, instructions, ""


def _get_photoroom_api_key():
    api_key = os.environ.get("PHOTOROOM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "PHOTOROOM_API_KEY not set. Set it in the environment for real background "
            "removal, or set RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    return api_key


def _photoroom_remove_background(image_bytes, *, ledger, submission_id):
    def _call():
        import requests

        resp = requests.post(
            "https://sdk.photoroom.com/v1/segment",
            headers={"x-api-key": _get_photoroom_api_key()},
            files={"image_file": ("image.jpg", image_bytes)},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    return _call_with_cost(ledger, submission_id, "images", "photoroom", PHOTOROOM_UNIT_COST_CAD, _call)


def _get_clipdrop_api_key():
    api_key = os.environ.get("CLIPDROP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "CLIPDROP_API_KEY not set. Set it in the environment for real tape/clutter "
            "cleanup, or set RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    return api_key


def _clipdrop_cleanup(image_bytes, mask_bytes, *, ledger, submission_id):
    """Inpaint ONLY the masked region (see _build_cleanup_mask) — the product's true
    edges, colour, printed text, and any real defects outside the mask are never
    sent through the model as editable pixels."""
    def _call():
        import requests

        resp = requests.post(
            "https://clipdrop-api.co/cleanup/v1",
            headers={"x-api-key": _get_clipdrop_api_key()},
            files={"image_file": ("image.png", image_bytes), "mask_file": ("mask.png", mask_bytes)},
            data={"mode": "quality"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content

    return _call_with_cost(ledger, submission_id, "images", "clipdrop_cleanup", CLIPDROP_UNIT_COST_CAD, _call)


def _gemini_remove_background(image_bytes, *, ledger, submission_id):
    from .providers import gemini

    def _call():
        return gemini.remove_background_image(image_bytes)

    return _call_with_cost(ledger, submission_id, "images", "gemini_bg_remove",
                            gemini.GEMINI_IMAGE_UNIT_COST_CAD, _call,
                            retry_exceptions=_GEMINI_RETRY_ERRORS)


def _gemini_inpaint_cleanup(marked_image_bytes, *, ledger, submission_id):
    from .providers import gemini

    def _call():
        return gemini.inpaint_region_image(marked_image_bytes)

    return _call_with_cost(ledger, submission_id, "images", "gemini_inpaint",
                            gemini.GEMINI_IMAGE_UNIT_COST_CAD, _call,
                            retry_exceptions=_GEMINI_RETRY_ERRORS)


def _bedrock_remove_background(image_bytes, *, ledger, submission_id):
    from .providers import bedrock

    _call = _bedrock_guard(lambda: bedrock.remove_background_image(image_bytes))

    return _call_with_cost(ledger, submission_id, "images", "bedrock_bg_remove",
                            bedrock.BEDROCK_IMAGE_UNIT_COST_CAD, _call,
                            retry_exceptions=_BEDROCK_RETRY_ERRORS)


def _bedrock_inpaint_cleanup(image_bytes, mask_bytes, *, ledger, submission_id):
    from .providers import bedrock

    _call = _bedrock_guard(lambda: bedrock.inpaint_region_image(image_bytes, mask_bytes))

    return _call_with_cost(ledger, submission_id, "images", "bedrock_inpaint",
                            bedrock.BEDROCK_IMAGE_UNIT_COST_CAD, _call,
                            retry_exceptions=_BEDROCK_RETRY_ERRORS)


def _get_s3_client(config=None):
    import boto3

    return boto3.client(
        "s3", region_name=os.environ.get("AWS_REGION", "ca-central-1"), config=config)


def _s3_put(client, key, data, content_type="image/jpeg"):
    client.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"


def _content_type_for(url):
    """Best-effort image content-type from a stored object URL's extension."""
    lower = url.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "application/octet-stream"


def _read_object_url(url):
    """Read the bytes of an object this pipeline previously wrote, from either
    backend, keyed off the scheme in its stored URL (`file://` for local-disk
    mode, `s3://<bucket>/<key>` for S3). Returns (bytes, content_type). Raises
    FileNotFoundError if the object is missing so callers can map it to a 404.

    The URL always originates from our own DB (a persisted *ImageURL field), never
    from client input, so there is no untrusted-path concern here."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        if not path.is_file():
            raise FileNotFoundError(url)
        return path.read_bytes(), _content_type_for(url)
    if parsed.scheme == "s3":
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        try:
            resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
        except Exception as exc:  # NoSuchKey and friends -> 404, not 500
            raise FileNotFoundError(url) from exc
        return resp["Body"].read(), resp.get("ContentType") or _content_type_for(url)
    raise FileNotFoundError(url)


LOCAL_IMAGE_DIR = os.environ.get("RESALE_LISTING_AI_LOCAL_IMAGE_DIR", "out/images")


def _connect_storage():
    """Decide S3 vs local-disk storage for one images-stage call. Tries a real
    S3 client + head_bucket; any failure (no creds, boto3 missing, wrong
    bucket, no network) falls back to local. One decision per submission --
    never a mid-run mix of S3 and local writes.

    The head_bucket probe uses its OWN short-timeout, no-retry client so an
    unreachable network fails fast into the local-mode fallback instead of
    blocking for minutes on botocore's default connect/retry behavior --
    this runs once per submission (image_process calls _connect_storage
    fresh each time), not once per process. The real client used for
    subsequent uploads (once S3 mode is confirmed) keeps default timeouts,
    since legitimate large uploads can take longer."""
    import botocore.config

    try:
        probe_client = _get_s3_client(config=botocore.config.Config(
            connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}))
        probe_client.head_bucket(Bucket=S3_BUCKET)
        client = _get_s3_client()  # real client for subsequent uploads, default timeouts
        return {"mode": "s3", "client": client}
    except Exception as exc:
        print(f"image storage: S3 unavailable ({exc}), falling back to local disk")
        return {"mode": "local", "client": None}


def _put_object(storage, key, data, content_type="image/jpeg"):
    if storage["mode"] == "s3":
        return _s3_put(storage["client"], key, data, content_type=content_type)
    path = Path(LOCAL_IMAGE_DIR) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"file://{path.resolve()}"


def _folder_url(storage, prefix):
    if storage["mode"] == "s3":
        return f"s3://{S3_BUCKET}/{prefix}"
    return f"file://{(Path(LOCAL_IMAGE_DIR) / prefix).resolve()}/"


def _empty_image_result():
    return {
        "hero": NULL(),
        "website_hero": NULL(), "website_hero_review": False, "website_hero_prompt": "",
        "website_hero_rejected": "",
        "supporting": [],
        "original_folder": NULL(), "scrubbed_folder": NULL(),
        "flags": _photo_availability_flags([]),
    }


def _stub_image_process(submission, product_key, ledger=None):
    sid = submission["submission_id"]
    images = submission.get("images", [])
    classified = [
        (path, {"category": "actual_product", "needs_cleanup": "tape" in str(path).lower()})
        for path in images
    ]
    chosen = _select_finished_images(classified) or [sid]
    hero = E(f"s3://{S3_BUCKET}/finished/{product_key}/hero.png", 0.86, "generated")
    supporting = [
        E(f"s3://{S3_BUCKET}/finished/{sid}/support_{i}.png", round(0.85 - 0.01 * (i - 1), 2), "generated")
        for i in range(1, len(chosen[1:4]) + 1)
    ] or [
        E(f"s3://{S3_BUCKET}/finished/{sid}/support_1.png", 0.85, "generated"),
        E(f"s3://{S3_BUCKET}/finished/{sid}/support_2.png", 0.84, "generated"),
    ]
    if ledger is not None:
        ledger.add(sid, "images", "photoroom", 0.050)
    return {
        "hero": hero,
        "website_hero": NULL(),
        "website_hero_review": False,
        "website_hero_prompt": "",
        "website_hero_rejected": "",
        "supporting": supporting,
        "original_folder": E(f"s3://{S3_BUCKET}/originals/{sid}/", 1.0, "derived"),
        "scrubbed_folder": E(f"s3://{S3_BUCKET}/scrubbed/{sid}/", 1.0, "derived"),
        "flags": _photo_availability_flags(classified),
    }


def image_process(submission, product_key, ledger=None, identity=None, already_has_hero=False,
                   already_has_website_hero=False, config=None):
    """Background-remove every finished image via the configured image_process
    provider (PhotoRoom+Clipdrop by default; Gemini when
    RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER selects it), then — ONLY for images an
    internal vision classification flags as containing a measuring tape/ruler/
    clutter — inpaint just that flagged region (Clipdrop Cleanup, or Gemini's
    marked-region inpaint on the gemini path), so the product's true edges/
    colour/printed text are preserved and real defect evidence elsewhere in
    the frame is never smoothed away. Uploads originals
    (immutable), scrubbed, and finished images to S3; returns one product-level
    hero + up to 3 unit-level supporting image URLs plus the four Listing
    photo-availability flags, derived from every submitted image.

    Hero derivation: an actual-product photo at index 0 of the chosen set
    becomes `hero` (a scrubbed submitted photo); otherwise `hero` stays NULL
    rather than falling back to an arbitrary submitted image -- FR-20's
    "define behaviour when evidence cannot support safe generation". The
    AI-generated `website_hero` (_generate_website_hero) is attempted
    separately whenever `identity` is given and `already_has_website_hero` is
    False (a product that already has a confident AI website hero never pays
    for another generation) -- this is gated on the website hero's OWN
    presence, deliberately independent of `already_has_hero` (the actual-item
    photo), since a resubmission can easily have one without the other."""
    if _stub_mode():
        return _stub_image_process(submission, product_key, ledger)

    sid = submission["submission_id"]
    images = submission.get("images", [])
    classified = []
    for path in images:
        print(f"[images] classifying {Path(path).name} ({len(classified) + 1}/{len(images)})",
              file=sys.stderr, flush=True)
        try:
            info = _classify_image(path, ledger=ledger, submission_id=sid, config=config)
        except (OSError, FileNotFoundError):
            continue
        classified.append((path, info))

    flags = _photo_availability_flags(classified)
    if not classified:
        return _empty_image_result()

    chosen = _select_finished_images(classified)
    by_path = dict(classified)
    storage = _connect_storage()

    provider = _image_process_provider(config)
    finished_urls = []
    first_finished_bytes = None
    for idx, path in enumerate(chosen, start=1):
        print(f"[images] processing {Path(path).name} ({idx}/{len(chosen)})", file=sys.stderr, flush=True)
        original_bytes = _resolve_image_path(path).read_bytes()
        _put_object(storage, f"originals/{sid}/{Path(path).name}", original_bytes)

        if provider == "gemini":
            scrubbed_bytes = _gemini_remove_background(original_bytes, ledger=ledger, submission_id=sid)
        elif provider == "bedrock":
            scrubbed_bytes = _bedrock_remove_background(original_bytes, ledger=ledger, submission_id=sid)
        else:
            scrubbed_bytes = _photoroom_remove_background(original_bytes, ledger=ledger, submission_id=sid)
        _put_object(storage, f"scrubbed/{sid}/{Path(path).name}", scrubbed_bytes)

        info = by_path[path]
        finished_bytes = scrubbed_bytes
        if info.get("needs_cleanup"):
            if provider == "gemini":
                marked_bytes = _draw_bbox_marker(scrubbed_bytes, info.get("clutter_bbox"))
                if marked_bytes is not None:
                    finished_bytes = _gemini_inpaint_cleanup(marked_bytes, ledger=ledger, submission_id=sid)
            elif provider == "bedrock":
                mask_bytes = _build_cleanup_mask(scrubbed_bytes, info.get("clutter_bbox"))
                if mask_bytes is not None:
                    finished_bytes = _bedrock_inpaint_cleanup(scrubbed_bytes, mask_bytes, ledger=ledger, submission_id=sid)
            elif os.environ.get("CLIPDROP_API_KEY"):
                mask_bytes = _build_cleanup_mask(scrubbed_bytes, info.get("clutter_bbox"))
                if mask_bytes is not None:
                    finished_bytes = _clipdrop_cleanup(scrubbed_bytes, mask_bytes, ledger=ledger, submission_id=sid)

        if idx == 1:
            first_finished_bytes = finished_bytes

        finished_urls.append(_put_object(storage, f"finished/{product_key}/{Path(path).name}", finished_bytes))

    hero_path = chosen[0] if chosen else None
    hero_is_actual_product = hero_path is not None and by_path.get(hero_path, {}).get("category") == "actual_product"

    if hero_is_actual_product:
        hero = E(finished_urls[0], 0.86, "generated")
        supporting_urls = finished_urls[1:4]
    else:
        hero = NULL()
        supporting_urls = finished_urls[:3]

    website_hero, website_hero_review, website_hero_prompt, website_hero_rejected = NULL(), False, "", ""
    if identity is not None and not already_has_website_hero:
        website_hero, website_hero_review, website_hero_prompt, website_hero_rejected = _generate_website_hero(
            identity, product_key, storage, first_finished_bytes,
            ledger=ledger, submission_id=sid, config=config)

    supporting = [E(u, 0.84, "generated") for u in supporting_urls]

    return {
        "hero": hero,
        "website_hero": website_hero,
        "website_hero_review": website_hero_review,
        "website_hero_prompt": website_hero_prompt,
        "website_hero_rejected": website_hero_rejected,
        "supporting": supporting,
        "original_folder": E(_folder_url(storage, f"originals/{sid}/"), 1.0, "derived"),
        "scrubbed_folder": E(_folder_url(storage, f"scrubbed/{sid}/"), 1.0, "derived"),
        "flags": flags,
    }


# ---- copy stage — listing copy, constrained to VERIFIED Product facts only ----
COPY_MODEL = os.environ.get("RESALE_LISTING_AI_COPY_MODEL", "gpt-4o-mini")
COPY_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_COPY_UNIT_COST_CAD", "0.02"))

_COPY_PROMPT = _load_prompt("copy")
COPY_FIELDS = _COPY_PROMPT["fields"]
COPY_JSON_SCHEMA = {
    "type": "object",
    "properties": {f: _COPY_PROMPT["field_schema"] for f in COPY_FIELDS},
    "required": COPY_FIELDS,
    "additionalProperties": False,
}
_validate_strict_schema(COPY_JSON_SCHEMA, "copy")
COPY_INSTRUCTIONS = _COPY_PROMPT["instructions"]


def _verified_facts(product):
    """Only fields that pass the sufficiency gate's confidence bar (envelope.
    confident) — nothing unverified is ever exposed to the copy model, so every
    claim it writes traces back to a fact we actually stand behind."""
    return {field: env["value"] for field, env in product.items() if confident(env)}


def _map_copy_output(raw):
    out = {}
    for field in COPY_FIELDS:
        value = raw.get(field) if isinstance(raw, dict) else None
        if isinstance(value, str) and value.strip():
            out[field] = E(value, 0.85, "generated")
    return out


def _stub_generate_copy(product, ledger=None, submission_id=None):
    """Preserves the original offline demo copy (local concatenation of verified
    fields only) so RESALE_LISTING_AI_STUBS=1 output/cost stay unchanged."""
    if ledger is not None:
        ledger.add(submission_id, "copy", "llm", 0.020)

    def v(f):
        return product[f]["value"] if confident(product[f]) else None

    brand, name, colour, model = v("Brand"), v("ProductName"), v("Colour"), v("ModelNumber")
    if not (brand and name):
        return {}
    return {
        "MasterTitle": E(" ".join(b for b in (brand, name, colour, model) if b), 0.9, "generated"),
        "ShortTitle": E(" ".join(b for b in (brand, name, colour) if b), 0.9, "generated"),
        "MasterDescription": E(f"{brand} {name}. Details reflect verified product data only.", 0.85, "generated"),
        "SearchTerms": E(", ".join(b for b in (brand, name, colour) if b), 0.8, "generated"),
    }


def generate_copy(product, ledger=None, submission_id=None, config=None):
    """Generate MasterTitle/ShortTitle/MasterDescription/BulletPoint1-5/SearchTerms/
    SEOKeywords from VERIFIED Product facts only (envelope.confident), via the
    configured copy provider (OpenAI by default; Gemini or OpenRouter when
    RESALE_LISTING_AI_COPY_PROVIDER selects them). Any field the model can't honestly
    write is omitted (never null-padded into the record); no confident
    Brand+ProductName -> nothing solid enough to write copy from, no API
    call, no fabrication."""
    if _stub_mode():
        return _stub_generate_copy(product, ledger, submission_id)

    facts = _verified_facts(product)
    if not (facts.get("Brand") and facts.get("ProductName")):
        return {}

    facts_text = f"VERIFIED PRODUCT FACTS (JSON):\n{json.dumps(facts)}"
    provider = _copy_provider(config)
    retry_exceptions = None

    if provider == "gemini":
        from .providers import gemini
        service, cost = "gemini_copy", gemini.GEMINI_UNIT_COST_CAD
        retry_exceptions = _GEMINI_RETRY_ERRORS
        instructions = _instructions_with_schema(COPY_INSTRUCTIONS, COPY_JSON_SCHEMA)
        _call = lambda: gemini.copy_json(instructions, facts_text)
    elif provider == "openrouter":
        from .providers import openrouter
        service, cost = "openrouter_copy", openrouter.OPENROUTER_UNIT_COST_CAD
        instructions = _instructions_with_schema(COPY_INSTRUCTIONS, COPY_JSON_SCHEMA)
        _call = lambda: openrouter.copy_json(instructions, facts_text)
    else:
        service, cost = "openai_copy", COPY_UNIT_COST_CAD

        def _call():
            client = _get_openai_client()
            response = client.responses.create(
                model=COPY_MODEL,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": COPY_INSTRUCTIONS},
                    {"type": "input_text", "text": facts_text},
                ]}],
                text={"format": {"type": "json_schema", "name": "listing_copy",
                                  "schema": COPY_JSON_SCHEMA, "strict": True}},
            )
            return response.output_text, _responses_usage(response, COPY_MODEL)

    raw_text = _call_with_cost(ledger, submission_id, "copy", service, cost, _call,
                                retry_exceptions=retry_exceptions)
    raw = repair_json(raw_text, COPY_JSON_SCHEMA, "listing_copy",
                       ledger=ledger, submission_id=submission_id, stage="copy", config=config)
    return _map_copy_output(raw)
