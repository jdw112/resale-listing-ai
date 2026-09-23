"""Gemini adapter — one of two comparison providers for the vision/classify/
copy/web-search/image_process seams (resale_listing_ai/adapters.py dispatches here when
RESALE_LISTING_AI_VISION_PROVIDER / RESALE_LISTING_AI_COPY_PROVIDER / RESALE_LISTING_AI_WEB_SEARCH_PROVIDER /
RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER selects "gemini" / "gemini_search"). These calls request JSON via prompt instructions + a loose
JSON response mode rather than strict schema mode: Gemini's google_search tool
cannot be combined with structured-output mode in the same call."""

import os

GEMINI_MODEL = os.environ.get("RESALE_LISTING_AI_GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_GEMINI_UNIT_COST_CAD", "0.00"))


def _get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Set it in the environment for real Gemini "
            "calls, switch RESALE_LISTING_AI_VISION_PROVIDER/RESALE_LISTING_AI_COPY_PROVIDER/"
            "RESALE_LISTING_AI_WEB_SEARCH_PROVIDER/RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER to a "
            "different provider, or set RESALE_LISTING_AI_STUBS=1 to run the offline "
            "stub adapters."
        )
    from google import genai

    return genai.Client(api_key=api_key)


def _image_part(image_path):
    from pathlib import Path

    from google.genai import types

    from ._shared import guess_mime_type

    data = Path(image_path).read_bytes()
    return types.Part.from_bytes(data=data, mime_type=guess_mime_type(image_path))


def vision_json(instructions, notes, images):
    """Product-identity extraction from photos. Returns raw response text for
    the caller to run through resale_listing_ai.json_repair.repair_json — never
    trusted directly."""
    from google.genai import types

    client = _get_gemini_client()
    contents = [instructions]
    if notes:
        contents.append(f"Submitter notes: {notes}")
    contents.extend(_image_part(path) for path in images)

    response = client.models.generate_content(
        model=GEMINI_MODEL, contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text


def classify_json(instructions, image):
    """Single-image classification (photo category / cleanup flag)."""
    from google.genai import types

    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL, contents=[instructions, _image_part(image)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text


def copy_json(instructions, facts_json_text):
    """Listing copy from verified Product facts only (text-only, no images)."""
    from google.genai import types

    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL, contents=[instructions, facts_json_text],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text


def web_search_json(instructions):
    """Web-search-with-citations via Gemini's google_search grounding tool.
    Cannot be combined with response_mime_type=json (a real Gemini API
    constraint) — JSON is requested via the prompt text instead, and the
    caller runs the result through repair_json same as every other seam."""
    from google.genai import types

    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL, contents=[instructions],
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    return response.text


GEMINI_IMAGE_MODEL = os.environ.get("RESALE_LISTING_AI_GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
GEMINI_IMAGE_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_GEMINI_IMAGE_UNIT_COST_CAD", "0.00"))

_BG_REMOVE_INSTRUCTIONS = (
    "Remove the background from this product photo entirely, leaving only the "
    "product itself on a plain white background. Do not alter, crop, recolor, or "
    "redraw the product in any way — preserve its exact edges, colour, printed "
    "text, and any visible wear or damage exactly as shown in the original."
)

_INPAINT_INSTRUCTIONS = (
    "This image has a red rectangle marker drawn on it. Inpaint over ONLY the "
    "contents inside the red rectangle to remove the clutter/tape/foreign object "
    "there, then remove the red rectangle line itself. Every pixel outside the "
    "rectangle must remain exactly unchanged — do not alter the product's edges, "
    "colour, printed text, or any real wear/damage visible elsewhere in the frame."
)


def _image_part_from_bytes(image_bytes, mime_type=None):
    """Wrap raw image bytes (not backed by a file path, so mimetypes.guess_type
    has nothing to sniff) into a Gemini Part. When mime_type isn't given
    explicitly, sniff the real format from the bytes via Pillow -- these bytes
    are frequently the submitter's original JPEG photo, and mislabeling a JPEG
    as image/png sends the wrong format declaration to the API."""
    from io import BytesIO

    from google.genai import types
    from PIL import Image as PILImage

    if mime_type is None:
        fmt = PILImage.open(BytesIO(image_bytes)).format  # "JPEG" / "PNG" / "WEBP" / ...
        mime_type = f"image/{fmt.lower()}" if fmt else "image/jpeg"
    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


def _first_image_bytes(response):
    """Extract the first inline image part from a Gemini image-edit response.
    No image part (text-only reply/refusal) -> raise, never silently degrade to
    treating a failed edit as a no-op success."""
    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        for part in content.parts or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data
    raise ValueError("Gemini image-edit response contained no image part")


def remove_background_image(image_bytes):
    """Background-remove a product photo via Gemini image editing. Returns raw
    image bytes for the caller to upload — never trusted directly, same posture
    as every other Gemini seam's raw-text return."""
    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=[_BG_REMOVE_INSTRUCTIONS, _image_part_from_bytes(image_bytes)],
    )
    return _first_image_bytes(response)


def inpaint_region_image(marked_image_bytes):
    """Inpaint ONLY the region marked with a drawn rectangle (see
    resale_listing_ai.adapters._draw_bbox_marker) — the visual-boundary equivalent of
    Clipdrop's pixel mask, since Gemini's image editing is prompt-driven rather
    than literally mask-constrained."""
    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=[_INPAINT_INSTRUCTIONS,
                  _image_part_from_bytes(marked_image_bytes, mime_type="image/png")],
    )
    return _first_image_bytes(response)


def generate_hero_image(instructions, reference_bytes=None):
    """Generate a product hero image from text instructions, optionally
    conditioned on a reference photo. Returns raw image bytes for the caller
    to normalize, QC, and upload -- never trusted directly. Raises if the model
    returns no image part (text-only reply / refusal), never silently degrading
    a failed generation into a no-op success."""
    client = _get_gemini_client()
    contents = [instructions]
    if reference_bytes is not None:
        contents.append(_image_part_from_bytes(reference_bytes))
    response = client.models.generate_content(model=GEMINI_IMAGE_MODEL, contents=contents)
    return _first_image_bytes(response)
