"""Deterministic, Pillow-only image geometry and quality primitives for the
product image scrubbing standard. Everything here is a pure function over
bytes in, bytes/numbers out -- no network calls, no API keys, no mocks
needed to test it. resale_listing_ai/image_quality.py orchestrates these into the
pass/fail decisions image_process() (resale_listing_ai/adapters.py) acts on.
"""

import os

CANVAS_SIZE = 2048  # spec S1: "2048 x 2048 pixels in a square 1:1 format"
FILL_FRACTION = 0.825  # spec S1: "filling approximately 80-85% of the frame" -- midpoint
JPEG_QUALITY = int(os.environ.get("RESALE_LISTING_AI_SCRUB_JPEG_QUALITY", "92"))


def compose_on_white_canvas(cutout_bytes, *, canvas_size=CANVAS_SIZE,
                             fill_fraction=FILL_FRACTION, quality=JPEG_QUALITY):
    """Composite a background-removed cutout (expected to carry an alpha
    channel) onto a canvas_size x canvas_size pure-white sRGB canvas, product
    centred and scaled (aspect ratio preserved, never stretched) to occupy
    fill_fraction of the frame. Returns encoded JPEG bytes with an embedded
    sRGB ICC profile (spec S1's "High-quality JPEG using the sRGB colour
    profile")."""
    from io import BytesIO

    from PIL import Image as PILImage, ImageCms

    cutout = PILImage.open(BytesIO(cutout_bytes)).convert("RGBA")

    # Trim the fully-transparent border so scaling is based on the product's
    # actual extent, not the cutout's raw canvas size.
    bbox = cutout.getbbox()
    trimmed = cutout.crop(bbox) if bbox else cutout

    target_extent = int(canvas_size * fill_fraction)
    scale = min(target_extent / trimmed.width, target_extent / trimmed.height)
    new_size = (max(1, round(trimmed.width * scale)), max(1, round(trimmed.height * scale)))
    resized = trimmed.resize(new_size, PILImage.LANCZOS)

    canvas = PILImage.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))
    offset = ((canvas_size - new_size[0]) // 2, (canvas_size - new_size[1]) // 2)
    canvas.alpha_composite(resized, dest=offset)

    final = canvas.convert("RGB")
    srgb_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    buf = BytesIO()
    final.save(buf, format="JPEG", quality=quality, icc_profile=srgb_profile)
    return buf.getvalue()
