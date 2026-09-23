"""Tests for resale_listing_ai/providers/photoroom_image.py — the Photoroom /v2/edit
hero-generation seam (text-to-image GENERATE mode without a reference, editWithAI
EDIT mode with one). requests.post is mocked; the multipart field construction
and the image/error handling are exercised for real.
"""
import pytest


class _FakeResponse:
    def __init__(self, content=b"png-bytes", status_code=200, content_type="image/png"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


def test_generate_hero_image_posts_prompt_and_returns_bytes(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = files
        return _FakeResponse(content=b"generated-png")

    monkeypatch.setattr(requests, "post", fake_post)

    out = photoroom_image.generate_hero_image("an unbranded door lock on white")

    assert out == b"generated-png"
    assert captured["url"].endswith("/v2/edit")
    assert captured["headers"]["x-api-key"] == "test-key"
    # text-only multipart: prompt field present, no input image file sent
    assert captured["files"]["imageFromPrompt.prompt"][1] == "an unbranded door lock on white"
    assert captured["files"]["imageFromPrompt.size"][1] == "SQUARE_HD"
    assert "imageFile" not in captured["files"] and "imageUrl" not in captured["files"]


def test_generate_hero_image_edits_reference_bytes(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    monkeypatch.setattr(photoroom_image, "PHOTOROOM_TEXT_REMOVAL_MODE", "ai.all")
    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None):
        captured["files"] = files
        return _FakeResponse(content=b"edited-png")

    monkeypatch.setattr(requests, "post", fake_post)

    out = photoroom_image.generate_hero_image("keep this product, unbranded, on white",
                                              reference_bytes=b"a-real-photo")

    assert out == b"edited-png"
    files = captured["files"]
    # EDIT mode: the real photo is uploaded as imageFile and editWithAI drives it
    assert files["imageFile"][1] == b"a-real-photo"
    assert files["editWithAI.mode"][1] == "ai.auto"
    assert files["editWithAI.prompt"][1] == "keep this product, unbranded, on white"
    # branding is stripped by the purpose-built textRemoval field, not just prose
    assert files["textRemoval.mode"][1] == "ai.all"
    # not the text-to-image path
    assert "imageFromPrompt.prompt" not in files


def test_generate_hero_image_reference_png_sniffed(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    captured = {}
    monkeypatch.setattr(
        requests, "post",
        lambda url, headers=None, files=None, timeout=None: captured.update(files=files)
        or _FakeResponse())

    png = b"\x89PNG\r\n\x1a\n" + b"rest"
    photoroom_image.generate_hero_image("p", reference_bytes=png)
    assert captured["files"]["imageFile"][0] == "reference.png"
    assert captured["files"]["imageFile"][2] == "image/png"


def test_generate_hero_image_text_removal_disablable(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    monkeypatch.setattr(photoroom_image, "PHOTOROOM_TEXT_REMOVAL_MODE", "")
    captured = {}
    monkeypatch.setattr(
        requests, "post",
        lambda url, headers=None, files=None, timeout=None: captured.update(files=files)
        or _FakeResponse())

    photoroom_image.generate_hero_image("p", reference_bytes=b"x")
    assert "textRemoval.mode" not in captured["files"]


def test_generate_hero_image_non_image_response_raises(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    monkeypatch.setattr(
        requests, "post",
        lambda url, headers=None, files=None, timeout=None: _FakeResponse(
            content=b'{"error":"x"}', content_type="application/json"))

    with pytest.raises(ValueError):
        photoroom_image.generate_hero_image("prompt")


def test_generate_hero_image_http_error_propagates(monkeypatch):
    import requests
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.setenv("PHOTOROOM_API_KEY", "test-key")
    monkeypatch.setattr(
        requests, "post",
        lambda url, headers=None, files=None, timeout=None: _FakeResponse(status_code=402))

    with pytest.raises(requests.HTTPError):
        photoroom_image.generate_hero_image("prompt")


def test_api_key_required(monkeypatch):
    from resale_listing_ai.providers import photoroom_image

    monkeypatch.delenv("PHOTOROOM_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        photoroom_image._api_key()
