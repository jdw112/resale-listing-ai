"""Tests for resale_listing_ai/providers/openai_image.py — the OpenAI hero-generation
seam. The OpenAI client is mocked (matches the mocking style elsewhere); the
b64 decode and the generate-vs-edit dispatch are exercised for real.
"""
import base64

import pytest


class _FakeImageDatum:
    def __init__(self, b64_json):
        self.b64_json = b64_json


class _FakeImagesResponse:
    def __init__(self, data):
        self.data = data


class _FakeImages:
    def __init__(self, image_bytes, no_data=False):
        self._b64 = base64.b64encode(image_bytes).decode() if not no_data else None
        self._no_data = no_data
        self.generate_calls = []
        self.edit_calls = []

    def _response(self):
        if self._no_data:
            return _FakeImagesResponse([])
        return _FakeImagesResponse([_FakeImageDatum(self._b64)])

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return self._response()

    def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        return self._response()


class _FakeOpenAIClient:
    def __init__(self, image_bytes=b"hero", no_data=False):
        self.images = _FakeImages(image_bytes, no_data=no_data)


def test_generate_hero_image_text_only_calls_generate(monkeypatch):
    from resale_listing_ai.providers import openai_image

    client = _FakeOpenAIClient(b"generated-bytes")
    monkeypatch.setattr(openai_image, "_client", lambda: client)

    out = openai_image.generate_hero_image("make a hero")

    assert out == b"generated-bytes"
    assert len(client.images.generate_calls) == 1
    assert client.images.generate_calls[0]["prompt"] == "make a hero"
    assert client.images.edit_calls == []  # no reference -> no edit call


def test_generate_hero_image_with_reference_calls_edit(monkeypatch):
    from resale_listing_ai.providers import openai_image

    client = _FakeOpenAIClient(b"edited-bytes")
    monkeypatch.setattr(openai_image, "_client", lambda: client)

    out = openai_image.generate_hero_image("make a hero", reference_bytes=b"\xff\xd8jpeg-ish")

    assert out == b"edited-bytes"
    assert len(client.images.edit_calls) == 1
    assert client.images.edit_calls[0]["prompt"] == "make a hero"
    # reference wrapped as (filename, bytes, content_type) with a jpeg content type
    assert client.images.edit_calls[0]["image"][2] == "image/jpeg"
    assert client.images.generate_calls == []


def test_reference_upload_sniffs_png():
    from resale_listing_ai.providers import openai_image

    png = b"\x89PNG\r\n\x1a\n" + b"rest"
    name, data, content_type = openai_image._reference_upload(png)
    assert content_type == "image/png"
    assert data == png


def test_generate_hero_image_raises_when_no_image_data(monkeypatch):
    from resale_listing_ai.providers import openai_image

    client = _FakeOpenAIClient(no_data=True)
    monkeypatch.setattr(openai_image, "_client", lambda: client)

    with pytest.raises(ValueError):
        openai_image.generate_hero_image("make a hero")
