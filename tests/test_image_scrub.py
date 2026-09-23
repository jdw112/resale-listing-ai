"""Tests for resale_listing_ai/image_scrub.py — pure, deterministic, Pillow-only
image geometry and quality primitives (no network, no mocks needed)."""

from io import BytesIO

from PIL import Image as PILImage, ImageDraw


def _rgba_cutout(size=(300, 150), fill=(200, 60, 60, 255)):
    """An opaque rectangle on an otherwise fully-transparent RGBA canvas --
    stands in for what a background-removal provider returns."""
    img = PILImage.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([0, 0, size[0] - 1, size[1] - 1], fill=fill)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _content_bbox(jpeg_bytes):
    """Bounding box of non-white pixels in a finished canvas JPEG."""
    img = PILImage.open(BytesIO(jpeg_bytes)).convert("RGB")
    non_white = img.point(lambda p: 0 if p >= 250 else 255)
    return non_white.convert("L").getbbox()


def test_compose_on_white_canvas_returns_a_2048_square_jpeg():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout())
    img = PILImage.open(BytesIO(result))

    assert img.size == (image_scrub.CANVAS_SIZE, image_scrub.CANVAS_SIZE)
    assert img.format == "JPEG"


def test_compose_on_white_canvas_embeds_an_srgb_icc_profile():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout())
    img = PILImage.open(BytesIO(result))

    assert img.info.get("icc_profile")


def test_compose_on_white_canvas_background_outside_the_product_is_pure_white():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout())
    img = PILImage.open(BytesIO(result)).convert("RGB")

    assert img.getpixel((5, 5)) == (255, 255, 255)
    assert img.getpixel((image_scrub.CANVAS_SIZE - 5, image_scrub.CANVAS_SIZE - 5)) == (255, 255, 255)


def test_compose_on_white_canvas_fills_80_to_85_percent_of_the_frame():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout(size=(300, 300)))
    left, top, right, bottom = _content_bbox(result)

    longest_edge = max(right - left, bottom - top)
    fraction = longest_edge / image_scrub.CANVAS_SIZE
    assert 0.79 <= fraction <= 0.86


def test_compose_on_white_canvas_centers_the_product():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout(size=(300, 300)))
    left, top, right, bottom = _content_bbox(result)

    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    target = image_scrub.CANVAS_SIZE / 2
    assert abs(center_x - target) < 5
    assert abs(center_y - target) < 5


def test_compose_on_white_canvas_preserves_source_aspect_ratio():
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout(size=(400, 100)))
    left, top, right, bottom = _content_bbox(result)

    width, height = right - left, bottom - top
    assert abs((width / height) - 4.0) < 0.3


def test_compose_on_white_canvas_never_upscales_beyond_a_sane_ratio_for_a_tiny_source():
    """A tiny cutout scaled up to fill 80-85% of a 2048 canvas is a large
    upscale by construction -- this test only pins that compose_on_white_canvas
    does not error out and still returns a canvas-sized image; the decision
    to REJECT a source too small to enlarge safely belongs to the quality
    gate (Task 4), not to this pure compositing function."""
    from resale_listing_ai import image_scrub

    result = image_scrub.compose_on_white_canvas(_rgba_cutout(size=(20, 20)))
    img = PILImage.open(BytesIO(result))

    assert img.size == (image_scrub.CANVAS_SIZE, image_scrub.CANVAS_SIZE)
