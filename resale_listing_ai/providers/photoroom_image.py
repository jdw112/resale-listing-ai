"""Photoroom image-generation seam for the AI website hero (resale_listing_ai/adapters.py
dispatches here when the hero_generate provider is "photoroom"). Mirrors the
gemini/openai generate_hero_image contract: instructions in, raw image bytes out
for the caller to normalize, QC, and upload -- never trusted directly.

Two modes on Photoroom's Image Editing API (POST /v2/edit), selected by whether
the caller supplies a reference image:

* reference_bytes given -> EDIT mode. Upload the real (already background-scrubbed)
  product photo as `imageFile` and drive `editWithAI` (mode ai.auto + prompt) so
  the output preserves the actual product geometry, parts, materials and colour
  instead of a from-scratch guess. `textRemoval.mode=ai.all` strips printed
  labels, logos, model numbers and decals off the product surfaces, which is the
  hero standard's un-branding requirement -- a purpose-built field rather than
  hoping the prompt alone erases them. This is the preferred path: a text-only
  prompt cannot reproduce a specific SKU the model has never seen.

* reference_bytes None -> GENERATE mode (`imageFromPrompt`), the text-to-image
  fallback. Used only when no reference is available.

Both /v2/edit and the classic scrubbing endpoint (sdk.photoroom.com/v1/segment)
authenticate with the same PHOTOROOM_API_KEY x-api-key header. editWithAI is a
Plus-plan feature; a key without the entitlement returns 4xx, which the caller
classifies as non-retryable."""
import os

PHOTOROOM_EDIT_URL = os.environ.get(
    "RESALE_LISTING_AI_PHOTOROOM_EDIT_URL", "https://image-api.photoroom.com/v2/edit")
PHOTOROOM_IMAGE_SIZE = os.environ.get("RESALE_LISTING_AI_PHOTOROOM_IMAGE_SIZE", "SQUARE_HD")
# textRemoval.mode for the edit path; ai.all removes both natural (printed-on-
# product) and artificial (overlaid) text. Set empty to disable text removal.
PHOTOROOM_TEXT_REMOVAL_MODE = os.environ.get(
    "RESALE_LISTING_AI_PHOTOROOM_TEXT_REMOVAL_MODE", "ai.all")


def _api_key():
    api_key = os.environ.get("PHOTOROOM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "PHOTOROOM_API_KEY not set. Set it for real hero-generation calls, or "
            "set RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    return api_key


def _reference_upload(reference_bytes):
    """multipart file tuple (filename, bytes, content-type) for the input photo,
    sniffing PNG vs JPEG from the magic bytes."""
    if reference_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ("reference.png", reference_bytes, "image/png")
    return ("reference.jpg", reference_bytes, "image/jpeg")


def _post(fields):
    """POST a multipart /v2/edit request and return raw image bytes. Raises
    requests.HTTPError on an error status (the caller classifies 4xx vs
    retryable), or ValueError if a 2xx response is not an image."""
    import requests

    resp = requests.post(
        PHOTOROOM_EDIT_URL,
        headers={"x-api-key": _api_key()},
        files=fields,
        timeout=60,
    )
    resp.raise_for_status()
    if not resp.headers.get("Content-Type", "").startswith("image/"):
        raise ValueError("Photoroom /v2/edit response was not an image")
    return resp.content


def generate_hero_image(instructions, reference_bytes=None):
    """Generate a product hero image via Photoroom's /v2/edit. When
    reference_bytes is supplied, EDIT the real product photo (editWithAI +
    textRemoval) so the output keeps the actual product; otherwise fall back to
    text-to-image generation from instructions. Returns raw image bytes."""
    if reference_bytes is not None:
        # (None, value) tuples force plain multipart/form-data fields; imageFile
        # carries the real photo. removeBackground stays false -- the caller's
        # normalize step composites onto pure white regardless, so we avoid a
        # redundant re-cut here.
        fields = {
            "imageFile": _reference_upload(reference_bytes),
            "editWithAI.mode": (None, "ai.auto"),
            "editWithAI.prompt": (None, instructions),
            "removeBackground": (None, "false"),
        }
        if PHOTOROOM_TEXT_REMOVAL_MODE:
            fields["textRemoval.mode"] = (None, PHOTOROOM_TEXT_REMOVAL_MODE)
        return _post(fields)

    fields = {
        "imageFromPrompt.prompt": (None, instructions),
        "imageFromPrompt.size": (None, PHOTOROOM_IMAGE_SIZE),
        "removeBackground": (None, "false"),
    }
    return _post(fields)
