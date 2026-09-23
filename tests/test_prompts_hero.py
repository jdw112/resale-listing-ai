import json
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "config" / "prompts"


def test_hero_generate_prompt_has_template_placeholders():
    data = json.loads((PROMPTS / "hero_generate.json").read_text())
    assert "{depiction}" in data["instructions"]
    assert "{identification}" in data["instructions"]
    assert "unbranded" in data["instructions"].lower()


def test_hero_qc_prompt_schema_shape():
    data = json.loads((PROMPTS / "hero_qc.json").read_text())
    assert "{query}" in data["instructions"]
    assert data["json_schema"]["required"] == [
        "branding_clear", "coherent", "matches_confirmed", "no_invented", "reasons"]
    assert data["json_schema"]["additionalProperties"] is False
