"""Tests for resale_listing_ai/providers/gemini.py — the Gemini comparison provider.
Client calls are mocked (matches tests/test_adapters.py's style); Part/Config
object construction is exercised for real against the installed google-genai
SDK, so a schema/field-name drift in that SDK surfaces here.
"""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SOME_IMAGE = str(FIXTURES / "barcode_ean13.png")
JPEG_IMAGE = str(FIXTURES / "blank_no_barcode.jpg")


def _fake_png_bytes():
    """Real (tiny) PNG bytes to use as *input* to remove_background_image /
    inpaint_region_image in tests that don't care about mime-type sniffing --
    _image_part_from_bytes sniffs the real format via Pillow, so a literal
    placeholder string like b"original-jpg-bytes" is no longer decodable."""
    from io import BytesIO

    from PIL import Image as PILImage

    buf = BytesIO()
    PILImage.new("RGB", (4, 4)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeGeminiModels:
    def __init__(self, text):
        self._text = text
        self.last_call = None

    def generate_content(self, **kwargs):
        self.last_call = kwargs
        return type("Response", (), {"text": self._text})()


class _FakeGeminiClient:
    def __init__(self, text):
        self.models = _FakeGeminiModels(text)


def test_get_gemini_client_requires_api_key(monkeypatch):
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        gemini._get_gemini_client()


def test_vision_json_sends_instructions_notes_and_images(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeGeminiClient('{"Brand": {"value": "Acme"}}')
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.vision_json("extract product facts", "40-foot model", [SOME_IMAGE])

    assert result == '{"Brand": {"value": "Acme"}}'
    assert fake_client.models.last_call["model"] == gemini.GEMINI_MODEL
    contents = fake_client.models.last_call["contents"]
    assert contents[0] == "extract product facts"
    assert "40-foot model" in contents[1]
    assert len(contents) == 3  # instructions + notes + one image part


def test_vision_json_omits_notes_when_absent(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeGeminiClient("{}")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    gemini.vision_json("extract product facts", None, [SOME_IMAGE])

    contents = fake_client.models.last_call["contents"]
    assert len(contents) == 2  # instructions + one image part, no notes


def test_classify_json_sends_one_image(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeGeminiClient(
        '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}')
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.classify_json("classify this photo", SOME_IMAGE)

    assert "actual_product" in result
    assert len(fake_client.models.last_call["contents"]) == 2


def test_copy_json_sends_instructions_and_facts_text(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeGeminiClient('{"MasterTitle": "Acme Smart Doorbell"}')
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.copy_json("write copy", '{"Brand": "Acme"}')

    assert "MasterTitle" in result
    assert fake_client.models.last_call["contents"] == ["write copy", '{"Brand": "Acme"}']


def test_web_search_json_enables_google_search_tool(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeGeminiClient(
        '{"matched": true, "confidence": 0.8, "match_description": "Smart Doorbell"}')
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.web_search_json("confirm this product via web search")

    assert "matched" in result
    config = fake_client.models.last_call["config"]
    assert config.tools is not None
    assert config.tools[0].google_search is not None


class _FakeImagePart:
    def __init__(self, data):
        self.inline_data = type("InlineData", (), {"data": data})()


class _FakeImageContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeImageCandidate:
    def __init__(self, parts, no_content=False):
        # A real Gemini safety-filtered refusal returns a candidate whose
        # .content is None -- no parts to walk at all.
        self.content = None if no_content else _FakeImageContent(parts)


class _FakeImageModels:
    def __init__(self, image_bytes, no_image=False, no_content=False):
        self._image_bytes = image_bytes
        self._no_image = no_image
        self._no_content = no_content
        self.last_call = None
        self.last_contents = None

    def generate_content(self, **kwargs):
        self.last_call = kwargs
        self.last_contents = kwargs.get("contents")
        parts = [] if self._no_image else [_FakeImagePart(self._image_bytes)]
        candidate = _FakeImageCandidate(parts, no_content=self._no_content)
        return type("Response", (), {"candidates": [candidate]})()


class _FakeImageClient:
    def __init__(self, image_bytes, no_image=False, no_content=False):
        self.models = _FakeImageModels(image_bytes, no_image=no_image, no_content=no_content)


def test_remove_background_image_returns_first_image_part(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"scrubbed-png-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.remove_background_image(_fake_png_bytes())

    assert result == b"scrubbed-png-bytes"
    assert fake_client.models.last_call["model"] == gemini.GEMINI_IMAGE_MODEL


def test_remove_background_image_raises_when_no_image_returned(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"", no_image=True)
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    with pytest.raises(ValueError):
        gemini.remove_background_image(_fake_png_bytes())


def test_inpaint_region_image_returns_first_image_part(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"cleaned-png-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    result = gemini.inpaint_region_image(_fake_png_bytes())

    assert result == b"cleaned-png-bytes"
    assert fake_client.models.last_call["model"] == gemini.GEMINI_IMAGE_MODEL


def test_inpaint_region_image_raises_when_no_image_returned(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"", no_image=True)
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    with pytest.raises(ValueError):
        gemini.inpaint_region_image(_fake_png_bytes())


def test_first_image_bytes_raises_value_error_when_candidate_content_is_none(monkeypatch):
    """A real Gemini safety-filtered refusal returns a candidate with
    content=None. That must surface as the intended, diagnostic ValueError --
    not an AttributeError from unconditionally walking candidate.content.parts."""
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"", no_content=True)
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    with pytest.raises(ValueError, match="no image part"):
        gemini.remove_background_image(_fake_png_bytes())


def test_remove_background_image_labels_real_jpeg_bytes_as_image_jpeg(monkeypatch):
    """The submitted photo bytes handed to remove_background_image are
    typically a real JPEG, never a PNG -- _image_part_from_bytes must sniff
    the actual format instead of hardcoding image/png (the bug: every real
    background-removal call would be sent mislabeled)."""
    from resale_listing_ai.providers import gemini

    jpeg_bytes = Path(JPEG_IMAGE).read_bytes()
    fake_client = _FakeImageClient(b"scrubbed-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    gemini.remove_background_image(jpeg_bytes)

    image_part = fake_client.models.last_call["contents"][1]
    assert image_part.inline_data.mime_type == "image/jpeg"


def test_inpaint_region_image_sends_image_png(monkeypatch):
    """inpaint_region_image's input is always the output of
    resale_listing_ai.adapters._draw_bbox_marker, which always produces PNG bytes --
    that guarantee should be load-bearing in the code (explicit mime_type),
    not incidental sniffing that happens to agree."""
    from resale_listing_ai.providers import gemini

    from io import BytesIO

    from PIL import Image as PILImage

    buf = BytesIO()
    PILImage.new("RGB", (10, 10)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    fake_client = _FakeImageClient(b"cleaned-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    gemini.inpaint_region_image(png_bytes)

    image_part = fake_client.models.last_call["contents"][1]
    assert image_part.inline_data.mime_type == "image/png"


def test_generate_hero_image_text_only_returns_first_image_part(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"generated-hero-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    out = gemini.generate_hero_image("make a hero")

    assert out == b"generated-hero-bytes"
    # text-only: exactly one content part (the instructions), no reference image
    assert len(fake_client.models.last_contents) == 1


def test_generate_hero_image_with_reference_sends_two_parts(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"generated-hero-bytes")
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)
    monkeypatch.setattr(gemini, "_image_part_from_bytes", lambda b, mime_type=None: ("ref", b))

    out = gemini.generate_hero_image("make a hero", reference_bytes=b"scrubbed")

    assert out == b"generated-hero-bytes"
    assert len(fake_client.models.last_contents) == 2


def test_generate_hero_image_raises_when_no_image_returned(monkeypatch):
    from resale_listing_ai.providers import gemini

    fake_client = _FakeImageClient(b"", no_image=True)
    monkeypatch.setattr(gemini, "_get_gemini_client", lambda: fake_client)

    with pytest.raises(ValueError):
        gemini.generate_hero_image("make a hero")
