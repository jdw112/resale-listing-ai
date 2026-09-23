"""Admin-editable configuration overrides -- the highest-priority tier for
provider selection, the sufficiency gate, and business-config JSON defaults
Precedence everywhere a key applies: admin override > env var > file/hardcoded default.

`load_overrides(store)` reads every row currently in admin_config_override and
returns {key: value} for whatever is actually overridden -- an empty dict when
nothing is, so every `get(config, key, default)` call below falls straight
through to today's exact behavior when no admin override exists. Deliberately
free of any import of pipeline.py/adapters.py/json_repair.py: those modules
import THIS module (to thread config through their own seams), so importing
any of them back here would be circular.
"""

import json
from pathlib import Path

from .records import LISTING_FIELDS, PRODUCT_FIELDS

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_PROVIDER_STACK = json.loads((CONFIG_DIR / "provider_stack.json").read_text())

_ALL_FIELDS = set(PRODUCT_FIELDS) | set(LISTING_FIELDS)


def _validate_provider(options_key):
    options = _PROVIDER_STACK[options_key]

    def _validate(value):
        if value not in options:
            raise ValueError(f"must be one of {options}")
    return _validate


def _validate_threshold(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("must be a number")
    if not (0.0 <= value <= 1.0):
        raise ValueError("must be between 0 and 1")


def _validate_field_list(value):
    if not isinstance(value, list) or not value:
        raise ValueError("must be a non-empty list of field names")
    unknown = [f for f in value if f not in _ALL_FIELDS]
    if unknown:
        raise ValueError(f"unknown field name(s): {unknown}")


def _validate_mandatory_fields(value):
    """gate.mandatory_fields is checked by pipeline.gate() against `identity`,
    a Product-shaped dict from adapters.vision_identify -- it can never contain
    a Listing-only field (e.g. SKU), so only PRODUCT_FIELDS are legal here
    (unlike gate.core_fields, which legitimately spans both Product and
    Listing fields -- see _validate_field_list above)."""
    if not isinstance(value, list) or not value:
        raise ValueError("must be a non-empty list of field names")
    unknown = [f for f in value if f not in PRODUCT_FIELDS]
    if unknown:
        raise ValueError(f"unknown field name(s): {unknown}")


def _validate_categories(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("must be a non-empty object of {category: [subcategories]}")
    for category, subcats in value.items():
        if not isinstance(category, str) or not category.strip():
            raise ValueError("every category name must be a non-empty string")
        if not isinstance(subcats, list) or not all(isinstance(s, str) and s.strip() for s in subcats):
            raise ValueError(f"category {category!r} must map to a list of non-empty strings")


def _validate_condition_scale(value):
    if not isinstance(value, dict):
        raise ValueError("must be an object")
    for key in ("provided_grades", "ai_suggested_grades"):
        grades = value.get(key)
        if not isinstance(grades, list) or not grades or not all(
                isinstance(g, str) and g.strip() for g in grades):
            raise ValueError(f"{key!r} must be a non-empty list of non-empty strings")


def _validate_oversized_rules(value):
    if not isinstance(value, dict):
        raise ValueError("must be an object")
    for key in ("longest_side_cm", "length_plus_girth_cm", "weight_kg"):
        num = value.get(key)
        if not isinstance(num, (int, float)) or isinstance(num, bool) or num <= 0:
            raise ValueError(f"{key!r} must be a positive number")


# key -> validator(value), raising ValueError with a human-readable message on
# bad input. The only place the set of admin-editable keys is defined --
# GET/PUT/DELETE /v1/admin/config and the frontend all key off this dict.
KEYS = {
    "provider_stack.vision_provider": _validate_provider("vision_options"),
    "provider_stack.copy_provider": _validate_provider("copy_options"),
    "provider_stack.web_search_provider": _validate_provider("options"),
    "provider_stack.image_process_provider": _validate_provider("image_process_options"),
    "provider_stack.json_repair_provider": _validate_provider("json_repair_options"),
    "gate.threshold": _validate_threshold,
    "gate.mandatory_fields": _validate_mandatory_fields,
    "gate.core_fields": _validate_field_list,
    "business.categories": _validate_categories,
    "business.condition_scale": _validate_condition_scale,
    "business.oversized_ups_ca": _validate_oversized_rules,
}


def validate_value(key, value):
    """Raises ValueError (human-readable) if `value` is not a legal override
    for `key`. Callers (resale_listing_ai/api.py) turn this into a 422."""
    if key not in KEYS:
        raise ValueError(f"unknown config key: {key!r}")
    KEYS[key](value)


def load_overrides(store):
    """{key: value} for every key currently overridden in
    admin_config_override -- always the already-injected `store` a caller was
    given, never a fresh Store.connect() (which would open its own in-memory
    sqlite DB and silently miss a test's injected data)."""
    return {row["key"]: json.loads(row["value_json"]) for row in store.get_admin_config_overrides()}


def get(config, key, default):
    """The one lookup every pipeline/adapter seam uses: an admin override (when
    `config` carries one for `key`) beats everything else; `config` being None
    or lacking `key` falls straight through to `default` -- today's exact
    behavior."""
    if config is None:
        return default
    return config.get(key, default)
