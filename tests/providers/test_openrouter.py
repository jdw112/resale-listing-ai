"""Tests for resale_listing_ai/providers/openrouter.py — the OpenRouter comparison
provider. OpenRouter is OpenAI-chat-completions-compatible, so the fake client
mirrors tests/test_adapters.py's _FakePerplexityClient exactly (same trick:
the openai package's own client shape, pointed at a different base_url)."""

from pathlib import Path

import httpx
import openai
import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SOME_IMAGE = str(FIXTURES / "barcode_ean13.png")


def _api_status_error(status_code, message):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status_code, request=request, json={"error": {"message": message}})
    return openai.APIStatusError(message, response=response, body=response.json())


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_call = None

    def create(self, **kwargs):
        self.last_call = kwargs
        return _FakeCompletionResponse(self._content)


class _FakeOpenRouterClient:
    def __init__(self, content):
        self.chat = type("chat", (), {})()
        self.chat.completions = _FakeCompletions(content)


class _FakeFailingCompletions:
    def __init__(self, error):
        self._error = error

    def create(self, **kwargs):
        raise self._error


class _FakeFailingOpenRouterClient:
    def __init__(self, error):
        self.chat = type("chat", (), {})()
        self.chat.completions = _FakeFailingCompletions(error)


def test_get_openrouter_client_requires_api_key(monkeypatch):
    """The API-key check must fire before the model check, so this test is
    valid regardless of whether RESALE_LISTING_AI_OPENROUTER_MODEL happens to be set
    in the environment pytest was started in."""
    from resale_listing_ai.providers import openrouter

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        openrouter._get_openrouter_client()


def test_get_openrouter_client_requires_model(monkeypatch):
    """OPENROUTER_MODEL has no hardcoded default (design spec: OpenRouter's
    catalog changes over time, so no vendor/model should be baked into code)
    -- it must be set explicitly via RESALE_LISTING_AI_OPENROUTER_MODEL, or this
    raises rather than silently calling the API with model=None."""
    from resale_listing_ai.providers import openrouter

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(openrouter, "OPENROUTER_MODEL", None)

    with pytest.raises(RuntimeError):
        openrouter._get_openrouter_client()


def test_vision_json_sends_instructions_notes_and_image(monkeypatch):
    from resale_listing_ai.providers import openrouter

    fake_client = _FakeOpenRouterClient('{"Brand": {"value": "Acme"}}')
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    result = openrouter.vision_json("extract product facts", "40-foot model", [SOME_IMAGE])

    assert result == '{"Brand": {"value": "Acme"}}'
    call = fake_client.chat.completions.last_call
    assert call["response_format"] == {"type": "json_object"}
    content = call["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "extract product facts"}
    assert content[1] == {"type": "text", "text": "Submitter notes: 40-foot model"}
    assert content[2]["type"] == "image_url"


def test_vision_json_omits_notes_when_absent(monkeypatch):
    from resale_listing_ai.providers import openrouter

    fake_client = _FakeOpenRouterClient("{}")
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    openrouter.vision_json("extract product facts", None, [SOME_IMAGE])

    content = fake_client.chat.completions.last_call["messages"][0]["content"]
    assert len(content) == 2  # instructions + one image, no notes


def test_classify_json_sends_one_image(monkeypatch):
    from resale_listing_ai.providers import openrouter

    fake_client = _FakeOpenRouterClient(
        '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}')
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    result = openrouter.classify_json("classify this photo", SOME_IMAGE)

    assert "actual_product" in result


def test_copy_json_sends_instructions_and_facts_text(monkeypatch):
    from resale_listing_ai.providers import openrouter

    fake_client = _FakeOpenRouterClient('{"MasterTitle": "Acme Smart Doorbell"}')
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    result = openrouter.copy_json("write copy", '{"Brand": "Acme"}')

    assert "MasterTitle" in result
    call = fake_client.chat.completions.last_call
    assert "write copy" in call["messages"][0]["content"]
    assert "Acme" in call["messages"][0]["content"]


def test_web_search_json_uses_online_suffixed_model(monkeypatch):
    from resale_listing_ai.providers import openrouter

    fake_client = _FakeOpenRouterClient(
        '{"matched": true, "confidence": 0.8, "match_description": "Smart Doorbell"}')
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    result = openrouter.web_search_json("confirm this product via web search")

    assert "matched" in result
    call = fake_client.chat.completions.last_call
    assert call["model"].endswith(":online")


def test_vision_json_raises_a_clear_error_on_413_payload_too_large(monkeypatch):
    """OpenRouter returns a 413 (not a rate limit) when the combined image
    payload exceeds its ~30MB limit -- this must surface as an actionable
    RuntimeError naming the images and the fix, not a raw APIStatusError."""
    from resale_listing_ai.providers import openrouter

    error = _api_status_error(413, "Downloaded image content cannot exceed 30MB")
    fake_client = _FakeFailingOpenRouterClient(error)
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    with pytest.raises(RuntimeError) as exc_info:
        openrouter.vision_json("extract product facts", None, [SOME_IMAGE, SOME_IMAGE])

    message = str(exc_info.value)
    assert "2 image(s)" in message
    assert "RESALE_LISTING_AI_MAX_IMAGES" in message
    assert "413" not in message.split("Original error:")[0]  # actionable text, not just the raw code


def test_classify_json_raises_a_clear_error_on_413_payload_too_large(monkeypatch):
    from resale_listing_ai.providers import openrouter

    error = _api_status_error(413, "Downloaded image content cannot exceed 30MB")
    fake_client = _FakeFailingOpenRouterClient(error)
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    with pytest.raises(RuntimeError) as exc_info:
        openrouter.classify_json("classify this photo", SOME_IMAGE)

    assert "1 image(s)" in str(exc_info.value)


def test_vision_json_reraises_non_413_api_status_errors_unchanged(monkeypatch):
    """Only the 413 payload-size case gets rewritten -- any other API status
    error (rate limits, auth, etc.) must propagate as-is so existing
    retry/error handling upstream still sees the real exception type."""
    from resale_listing_ai.providers import openrouter

    error = _api_status_error(402, "Insufficient credits")
    fake_client = _FakeFailingOpenRouterClient(error)
    monkeypatch.setattr(openrouter, "_get_openrouter_client", lambda: fake_client)

    with pytest.raises(openai.APIStatusError) as exc_info:
        openrouter.vision_json("extract product facts", None, [SOME_IMAGE])

    assert exc_info.value is error
