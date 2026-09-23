"""OpenRouter adapter — the other comparison provider for the vision/classify/
copy/web-search seams (resale_listing_ai/adapters.py dispatches here when
RESALE_LISTING_AI_VISION_PROVIDER / RESALE_LISTING_AI_COPY_PROVIDER / RESALE_LISTING_AI_WEB_SEARCH_PROVIDER
selects "openrouter" / "openrouter_online"). OpenRouter is OpenAI-chat-
completions-compatible — this reuses the already-installed `openai` package
with a different base_url, the same trick adapters.py's _get_perplexity_client
already uses, so no new dependency is needed for this provider."""

import os
from base64 import b64encode
from pathlib import Path

from ._shared import guess_mime_type

# No hardcoded default: OpenRouter's model catalog changes over time (design
# spec) -- must be set via RESALE_LISTING_AI_OPENROUTER_MODEL to whatever model id the
# user picks in their OpenRouter dashboard, e.g. "google/gemini-2.0-flash-001"
# or "openai/gpt-4o-mini". None until set -> _get_openrouter_client() below
# raises a clear error rather than silently calling the API with model=None.
OPENROUTER_MODEL = os.environ.get("RESALE_LISTING_AI_OPENROUTER_MODEL")
OPENROUTER_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_OPENROUTER_UNIT_COST_CAD", "0.01"))


def _get_openrouter_client():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Set it in the environment for real "
            "OpenRouter calls, switch RESALE_LISTING_AI_VISION_PROVIDER/RESALE_LISTING_AI_COPY_PROVIDER/"
            "RESALE_LISTING_AI_WEB_SEARCH_PROVIDER to a different provider, or set "
            "RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    if not OPENROUTER_MODEL:
        raise RuntimeError(
            "RESALE_LISTING_AI_OPENROUTER_MODEL not set. OpenRouter routes to many "
            "vendors' models, so there is no default -- set it to a model id "
            "from your OpenRouter dashboard, e.g. "
            "RESALE_LISTING_AI_OPENROUTER_MODEL=google/gemini-2.0-flash-001."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")


def _image_data_url(image_path):
    data = Path(image_path).read_bytes()
    mime = guess_mime_type(image_path)
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def _raise_if_payload_too_large(e, images):
    """OpenRouter returns a 413 (not a rate limit) when the combined image
    payload for a request is too large -- turn its generic message into one
    that names the actual images and how to fix it, instead of a raw
    APIStatusError traceback."""
    import openai

    if isinstance(e, openai.APIStatusError) and e.status_code == 413:
        total_bytes = sum(Path(image).stat().st_size for image in images)
        raise RuntimeError(
            f"OpenRouter rejected this request: the {len(images)} image(s) "
            f"total {total_bytes / 1_000_000:.1f}MB raw (more once base64-"
            f"encoded), over its ~30MB combined image-payload limit. Send "
            f"fewer or smaller images -- e.g. RESALE_LISTING_AI_MAX_IMAGES=<n> if "
            f"you're pointing run_live_demo.py at a directory. "
            f"Original error: {e}"
        ) from e
    raise e


def vision_json(instructions, notes, images):
    """Product-identity extraction from photos. Returns raw response text for
    the caller to run through resale_listing_ai.json_repair.repair_json."""
    import openai

    client = _get_openrouter_client()
    content = [{"type": "text", "text": instructions}]
    if notes:
        content.append({"type": "text", "text": f"Submitter notes: {notes}"})
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(image)}})

    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
    except openai.APIStatusError as e:
        _raise_if_payload_too_large(e, images)
    return response.choices[0].message.content


def classify_json(instructions, image):
    """Single-image classification (photo category / cleanup flag)."""
    import openai

    client = _get_openrouter_client()
    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": instructions},
                {"type": "image_url", "image_url": {"url": _image_data_url(image)}},
            ]}],
            response_format={"type": "json_object"},
        )
    except openai.APIStatusError as e:
        _raise_if_payload_too_large(e, [image])
    return response.choices[0].message.content


def copy_json(instructions, facts_json_text):
    """Listing copy from verified Product facts only (text-only, no images)."""
    client = _get_openrouter_client()
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": f"{instructions}\n\n{facts_json_text}"}],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def web_search_json(instructions):
    """Web-search-with-citations via OpenRouter's built-in web plugin (the
    ":online" model-name suffix)."""
    client = _get_openrouter_client()
    response = client.chat.completions.create(
        model=f"{OPENROUTER_MODEL}:online",
        messages=[{"role": "user", "content": instructions}],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content
