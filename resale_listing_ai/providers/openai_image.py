"""OpenAI image-generation seam for the AI website hero (resale_listing_ai/adapters.py
dispatches here when the hero_generate provider is "openai"). Mirrors
providers/gemini.py's generate_hero_image contract: text instructions in, raw
image bytes out for the caller to normalize, QC, and upload -- never trusted
directly. Uses gpt-image-1 by default (image-conditioned edits supported, so the
accuracy retry can pass the scrubbed reference photo)."""
import base64
import os

OPENAI_IMAGE_MODEL = os.environ.get("RESALE_LISTING_AI_OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_IMAGE_SIZE = os.environ.get("RESALE_LISTING_AI_OPENAI_IMAGE_SIZE", "1024x1024")


def _client():
    """Build an OpenAI client from OPENAI_API_KEY. Self-contained (does not import
    adapters) to avoid a circular import; mirrors adapters._get_openai_client."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Set it for real hero-generation calls, or "
            "set RESALE_LISTING_AI_STUBS=1 to run the offline stub adapters."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _reference_upload(reference_bytes):
    """Wrap raw reference bytes as an (filename, bytes, content_type) tuple for
    images.edit, sniffing PNG vs JPEG so the declared content type matches the
    bytes (the scrubbed reference is usually a JPEG)."""
    if reference_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ("reference.png", reference_bytes, "image/png")
    return ("reference.jpg", reference_bytes, "image/jpeg")


def _first_image_bytes(response):
    """Extract the first image from an OpenAI images response as raw bytes. No
    image data (empty/malformed response) -> raise, never silently degrade a
    failed generation into a no-op success (same posture as the Gemini seam)."""
    data = getattr(response, "data", None)
    if data:
        b64 = getattr(data[0], "b64_json", None)
        if b64:
            return base64.b64decode(b64)
    raise ValueError("OpenAI image response contained no image data")


def generate_hero_image(instructions, reference_bytes=None):
    """Generate a product hero image from text instructions, optionally
    conditioned on a reference photo (via images.edit). Returns raw image bytes
    for the caller to normalize and QC. Raises if the response carries no image."""
    client = _client()
    if reference_bytes is not None:
        response = client.images.edit(
            model=OPENAI_IMAGE_MODEL,
            image=_reference_upload(reference_bytes),
            prompt=instructions,
            size=OPENAI_IMAGE_SIZE,
        )
    else:
        response = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=instructions,
            size=OPENAI_IMAGE_SIZE,
        )
    return _first_image_bytes(response)
