"""Tests for resale_listing_ai/providers/bedrock.py — the Bedrock comparison provider.
Client calls are mocked (matches tests/providers/test_gemini.py's style)."""

import json
from base64 import b64decode, b64encode
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SOME_IMAGE = str(FIXTURES / "barcode_ean13.png")


class _FakeBedrockRuntimeClient:
    def __init__(self, text):
        self._text = text
        self.last_call = None

    def converse(self, **kwargs):
        self.last_call = kwargs
        return {"output": {"message": {"content": [{"text": self._text}]}}}


def test_get_bedrock_client_requires_credentials(monkeypatch):
    """Neither a Bedrock API key nor standard AWS creds -> RuntimeError."""
    import boto3

    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    class _FakeSession:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", _FakeSession)

    with pytest.raises(RuntimeError):
        bedrock._get_bedrock_client()


def test_get_bedrock_client_accepts_bearer_token_alone(monkeypatch):
    """AWS_BEARER_TOKEN_BEDROCK (the Bedrock API key) is sufficient on
    its own -- no standard AWS sigv4 credentials required. Client creation must
    not even consult boto3.Session().get_credentials() in this path (asserted
    by making that path raise if called)."""
    import boto3

    from resale_listing_ai.providers import bedrock

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key-xyz")

    class _ExplodingSession:
        def get_credentials(self):
            raise AssertionError("must not check sigv4 credentials when a bearer token is set")

    monkeypatch.setattr(boto3, "Session", _ExplodingSession)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object())

    client = bedrock._get_bedrock_client()

    assert client is not None


def test_get_bedrock_client_falls_back_to_standard_aws_credentials(monkeypatch):
    """No bearer token, but standard AWS creds resolve -> client created, no error."""
    import boto3

    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    class _FakeSession:
        def get_credentials(self):
            return object()  # any non-None value means "resolved"

    monkeypatch.setattr(boto3, "Session", _FakeSession)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object())

    client = bedrock._get_bedrock_client()

    assert client is not None


def test_vision_json_sends_instructions_notes_and_images(monkeypatch):
    from resale_listing_ai.providers import bedrock

    fake_client = _FakeBedrockRuntimeClient('{"Brand": {"value": "Acme"}}')
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    result = bedrock.vision_json("extract product facts", "40-foot model", [SOME_IMAGE])

    assert result == '{"Brand": {"value": "Acme"}}'
    assert fake_client.last_call["modelId"] == bedrock.BEDROCK_MODEL
    content = fake_client.last_call["messages"][0]["content"]
    assert content[0] == {"text": "extract product facts"}
    assert content[1] == {"text": "Submitter notes: 40-foot model"}
    assert len(content) == 3  # instructions + notes + one image block
    assert content[2]["image"]["format"] in ("png", "jpeg", "gif", "webp")


def test_vision_json_omits_notes_when_absent(monkeypatch):
    from resale_listing_ai.providers import bedrock

    fake_client = _FakeBedrockRuntimeClient("{}")
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    bedrock.vision_json("extract product facts", None, [SOME_IMAGE])

    content = fake_client.last_call["messages"][0]["content"]
    assert len(content) == 2  # instructions + one image block, no notes


def test_classify_json_sends_one_image(monkeypatch):
    from resale_listing_ai.providers import bedrock

    fake_client = _FakeBedrockRuntimeClient(
        '{"category": "actual_product", "needs_cleanup": false, "clutter_bbox": null}')
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    result = bedrock.classify_json("classify this photo", SOME_IMAGE)

    assert "actual_product" in result
    assert len(fake_client.last_call["messages"][0]["content"]) == 2


# ---- Converse image-format handling: Bedrock accepts ONLY png/jpeg/gif/webp ----


def _write_image(tmp_path, name, fmt, size=(64, 48), color=(10, 120, 200)):
    """A REAL file in `fmt` (no mocking): the whole point of these tests is that
    the provider inspects decoded content, not the filename."""
    from PIL import Image as PILImage

    path = tmp_path / name
    PILImage.new("RGB", size, color).save(str(path), format=fmt)
    return path


def _sent_block_image(fake_client, index=1):
    return fake_client.last_call["messages"][0]["content"][index]["image"]


def test_image_block_converts_a_heic_ipad_photo_to_jpeg(monkeypatch, tmp_path):
    """iPad/iPhone cameras produce .heic by default and those submissions are
    expected to work. HEIC is NOT a Bedrock-accepted Converse format, so it must
    be decoded (pillow-heif) and re-encoded as real JPEG bytes before the call."""
    from io import BytesIO

    from PIL import Image as PILImage

    from resale_listing_ai.providers import bedrock

    bedrock._register_heif_opener()  # so this test's own fixture can be written
    heic_path = _write_image(tmp_path, "ipad_photo.heic", "HEIF")
    assert PILImage.open(str(heic_path)).format == "HEIF"  # a genuine HEIC fixture

    fake_client = _FakeBedrockRuntimeClient("{}")
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    bedrock.vision_json("extract product facts", None, [str(heic_path)])

    block = _sent_block_image(fake_client)
    assert block["format"] == "jpeg"
    sent = PILImage.open(BytesIO(block["source"]["bytes"]))
    assert sent.format == "JPEG"          # really re-encoded, not relabelled HEIC
    assert sent.size == (64, 48)


def test_image_block_passes_an_already_jpeg_photo_through_untouched(monkeypatch, tmp_path):
    """An accepted format must not be re-encoded: a needless JPEG->JPEG round
    trip only costs quality."""
    from resale_listing_ai.providers import bedrock

    jpeg_path = _write_image(tmp_path, "photo.jpg", "JPEG")
    original = jpeg_path.read_bytes()

    fake_client = _FakeBedrockRuntimeClient("{}")
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    bedrock.vision_json("extract product facts", None, [str(jpeg_path)])

    block = _sent_block_image(fake_client)
    assert block["format"] == "jpeg"
    assert block["source"]["bytes"] == original  # byte-identical


def test_image_block_passes_an_already_png_photo_through_untouched(monkeypatch, tmp_path):
    from resale_listing_ai.providers import bedrock

    png_path = _write_image(tmp_path, "photo.png", "PNG")
    original = png_path.read_bytes()

    fake_client = _FakeBedrockRuntimeClient("{}")
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda: fake_client)

    bedrock.classify_json("classify this photo", str(png_path))

    block = _sent_block_image(fake_client)
    assert block["format"] == "png"
    assert block["source"]["bytes"] == original


def test_image_block_refuses_an_undecodable_file_instead_of_guessing(tmp_path):
    """Never fabricate a format for content Pillow cannot read — a guess would
    buy a billed ValidationException from AWS and hide the real cause."""
    from resale_listing_ai.providers import bedrock

    junk = tmp_path / "not_really.jpg"
    junk.write_bytes(b"this is not an image")

    with pytest.raises(ValueError) as excinfo:
        bedrock._image_block(str(junk))

    assert "not_really.jpg" in str(excinfo.value)


def test_default_model_id_is_a_region_prefixed_inference_profile(monkeypatch):
    """Claude 4.x on Bedrock cannot be invoked on-demand by its BASE model id --
    that returns ValidationException ("...isn't supported. Retry your request
    with the ID or ARN of an inference profile"). The default must be the
    cross-region inference profile id matching the default region."""
    import importlib

    from resale_listing_ai.providers import bedrock

    monkeypatch.delenv("RESALE_LISTING_AI_BEDROCK_MODEL", raising=False)
    monkeypatch.delenv("RESALE_LISTING_AI_BEDROCK_REGION", raising=False)
    try:
        importlib.reload(bedrock)

        assert bedrock.BEDROCK_REGION == "us-east-1"
        assert bedrock.BEDROCK_MODEL == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    finally:
        importlib.reload(bedrock)


class _FakeStreamingBody:
    def __init__(self, payload_bytes):
        self._payload_bytes = payload_bytes

    def read(self):
        return self._payload_bytes


class _FakeBedrockImageClient:
    def __init__(self, image_b64=None, no_image=False):
        self._image_b64 = image_b64
        self._no_image = no_image
        self.last_call = None

    def invoke_model(self, **kwargs):
        self.last_call = kwargs
        images = [] if self._no_image else [self._image_b64]
        payload = json.dumps({"images": images}).encode("utf-8")
        return {"body": _FakeStreamingBody(payload)}


def _png(size=(640, 480), color=(120, 120, 120), mode="RGB"):
    """A real PNG — the Nova Canvas paths open, resize and re-encode their
    inputs with Pillow, so opaque placeholder byte strings no longer suffice."""
    from io import BytesIO

    from PIL import Image as PILImage

    buf = BytesIO()
    PILImage.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _clipdrop_style_mask(size, box):
    """Exactly what adapters._build_cleanup_mask emits: an "L"-mode PNG, black
    field with the clutter box painted WHITE (Clipdrop's "white = clean this")."""
    from io import BytesIO

    from PIL import Image as PILImage
    from PIL import ImageDraw

    mask = PILImage.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle(list(box), fill=255)
    buf = BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def _sent_image(fake_client, params_key, field):
    from io import BytesIO

    from PIL import Image as PILImage

    body = json.loads(fake_client.last_call["body"])
    return PILImage.open(BytesIO(b64decode(body[params_key][field])))


def _image_client(monkeypatch, bedrock, **kwargs):
    fake_client = _FakeBedrockImageClient(**kwargs)
    monkeypatch.setattr(bedrock, "_get_bedrock_client", lambda config=None: fake_client)
    return fake_client


def test_remove_background_image_returns_decoded_bytes(monkeypatch):
    from resale_listing_ai.providers import bedrock

    encoded = b64encode(b"scrubbed-png-bytes").decode("ascii")
    fake_client = _image_client(monkeypatch, bedrock, image_b64=encoded)

    result = bedrock.remove_background_image(_png())

    assert result == b"scrubbed-png-bytes"
    assert fake_client.last_call["modelId"] == bedrock.BEDROCK_IMAGE_MODEL
    body = json.loads(fake_client.last_call["body"])
    assert body["taskType"] == "BACKGROUND_REMOVAL"


def test_remove_background_image_raises_when_no_image_returned(monkeypatch):
    from resale_listing_ai.providers import bedrock

    _image_client(monkeypatch, bedrock, no_image=True)

    with pytest.raises(ValueError):
        bedrock.remove_background_image(_png())


def test_inpaint_region_image_sends_mask_and_returns_decoded_bytes(monkeypatch):
    from resale_listing_ai.providers import bedrock

    encoded = b64encode(b"cleaned-png-bytes").decode("ascii")
    fake_client = _image_client(monkeypatch, bedrock, image_b64=encoded)

    result = bedrock.inpaint_region_image(_png(), _clipdrop_style_mask((640, 480), (10, 10, 100, 100)))

    assert result == b"cleaned-png-bytes"
    body = json.loads(fake_client.last_call["body"])
    assert body["taskType"] == "INPAINTING"
    assert "maskImage" in body["inPaintingParams"]


def test_inpaint_region_image_raises_when_no_image_returned(monkeypatch):
    from resale_listing_ai.providers import bedrock

    _image_client(monkeypatch, bedrock, no_image=True)

    with pytest.raises(ValueError):
        bedrock.inpaint_region_image(_png(), _clipdrop_style_mask((640, 480), (10, 10, 100, 100)))


# ---- Nova Canvas request-body contract (validated against AWS's published
# ---- API reference, not against the sibling Gemini provider's shape) ----


def test_inpaint_mask_polarity_is_inverted_for_nova_canvas(monkeypatch):
    """Nova Canvas: "Areas to be edited are shaded pure black and areas to
    ignore are shaded pure white" -- the OPPOSITE of _build_cleanup_mask's
    Clipdrop polarity. Uninverted, Nova would preserve the measuring tape and
    regenerate the product. Decode the actual pixels: asserting the field is
    merely present cannot tell the two polarities apart."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"cleaned").decode("ascii"))
    box = (100, 100, 200, 200)  # the clutter, WHITE in the mask we hand in

    bedrock.inpaint_region_image(_png((640, 480)), _clipdrop_style_mask((640, 480), box))

    mask = _sent_image(fake_client, "inPaintingParams", "maskImage")
    assert mask.mode == "RGB"  # AWS specifies an RGB mask, not single-channel "L"
    assert mask.getpixel((150, 150)) == (0, 0, 0)      # inside the clutter box -> EDIT
    assert mask.getpixel((10, 10)) == (255, 255, 255)  # the product -> IGNORE
    assert mask.getpixel((600, 450)) == (255, 255, 255)


def test_inpaint_omits_text_prompt(monkeypatch):
    """AWS: omitting `text` makes the model "remove elements inside the masked
    area ... replaced with a seamless extension of the image background" -- the
    exact wanted behaviour -- and AWS warns against the negating phrasing a
    hand-written cleanup prompt needs ("no measuring tape, ruler, clutter")."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"cleaned").decode("ascii"))

    bedrock.inpaint_region_image(_png(), _clipdrop_style_mask((640, 480), (10, 10, 100, 100)))

    params = json.loads(fake_client.last_call["body"])["inPaintingParams"]
    assert "text" not in params
    assert "negativeText" not in params
    assert set(params) == {"image", "maskImage"}


def test_inpaint_flattens_transparency_from_background_removal(monkeypatch):
    """BACKGROUND_REMOVAL returns "a PNG image with full 8-bit transparency",
    but an INPAINTING input image "must not contain any transparent or
    translucent pixels" -- so the chained scrubbed bytes must be flattened."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"cleaned").decode("ascii"))
    scrubbed = _png((640, 480), color=(0, 0, 0, 0), mode="RGBA")  # fully transparent

    bedrock.inpaint_region_image(scrubbed, _clipdrop_style_mask((640, 480), (10, 10, 100, 100)))

    sent = _sent_image(fake_client, "inPaintingParams", "image")
    assert sent.mode == "RGB"
    assert "transparency" not in sent.info
    assert sent.getpixel((300, 200)) == (255, 255, 255)  # composited onto opaque white


def test_remove_background_downscales_an_oversized_phone_photo(monkeypatch):
    """Nova Canvas rejects inputs over 4,194,304 total pixels; a stock 4032x3024
    phone photo is 12.2 MP."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"scrubbed").decode("ascii"))

    bedrock.remove_background_image(_png((4032, 3024)))

    sent = _sent_image(fake_client, "backgroundRemovalParams", "image")
    width, height = sent.size
    assert width * height <= bedrock.NOVA_CANVAS_MAX_PIXELS
    assert max(width, height) <= bedrock.NOVA_CANVAS_MAX_SIDE
    assert min(width, height) >= bedrock.NOVA_CANVAS_MIN_SIDE
    assert abs((width / height) - (4032 / 3024)) < 0.01  # aspect ratio preserved


def test_remove_background_sends_a_compliant_image_untouched(monkeypatch):
    """Never resize (and never re-encode) an image that already fits."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"scrubbed").decode("ascii"))
    original = _png((1024, 768))

    bedrock.remove_background_image(original)

    body = json.loads(fake_client.last_call["body"])
    assert b64decode(body["backgroundRemovalParams"]["image"]) == original


def test_inpaint_downscales_image_and_mask_together(monkeypatch):
    """If the image is resized the mask MUST be resized identically, or the
    mask stops covering the clutter (and Nova rejects a size mismatch)."""
    from resale_listing_ai.providers import bedrock

    fake_client = _image_client(monkeypatch, bedrock, image_b64=b64encode(b"cleaned").decode("ascii"))
    # Clutter box = the right half of the frame, so the scaled-down mask is
    # still checkable by relative position.
    bedrock.inpaint_region_image(_png((4032, 3024)),
                                  _clipdrop_style_mask((4032, 3024), (2016, 0, 4032, 3024)))

    sent = _sent_image(fake_client, "inPaintingParams", "image")
    mask = _sent_image(fake_client, "inPaintingParams", "maskImage")
    width, height = sent.size
    assert mask.size == sent.size
    assert width * height <= bedrock.NOVA_CANVAS_MAX_PIXELS
    # Polarity + placement survive the resize, and the resize keeps the mask
    # pure black/white (NEAREST, not an interpolating filter).
    assert mask.getpixel((int(width * 0.75), height // 2)) == (0, 0, 0)
    assert mask.getpixel((int(width * 0.25), height // 2)) == (255, 255, 255)


def test_nova_canvas_target_size_only_ever_scales_down():
    from resale_listing_ai.providers import bedrock

    assert bedrock._nova_canvas_target_size(800, 600) is None      # already fits
    assert bedrock._nova_canvas_target_size(320, 320) is None      # small: never upscaled
    assert bedrock._nova_canvas_target_size(6000, 100) is None     # 60:1 -> aspect-rejected, not cropped

    # Exactly ON the pixel cap still has to come down: the limit is "fewer than".
    on_the_cap = bedrock._nova_canvas_target_size(4096, 1024)
    assert on_the_cap[0] * on_the_cap[1] < bedrock.NOVA_CANVAS_MAX_PIXELS

    # Under the pixel cap but over the per-side limit -> still resized.
    long_side = bedrock._nova_canvas_target_size(8000, 1000)
    assert max(long_side) <= bedrock.NOVA_CANVAS_MAX_SIDE
    assert long_side[0] * long_side[1] < bedrock.NOVA_CANVAS_MAX_PIXELS


def test_image_client_raises_the_read_timeout_above_boto_default(monkeypatch):
    """AWS recommends >=300s for Nova Canvas generation; boto3 defaults to 60s,
    and a read timeout would surface as a retryable ClientError and be billed
    on every retry. The Converse/vision client keeps the default."""
    from resale_listing_ai.providers import bedrock

    seen = {}

    def _fake_get_client(config=None):
        seen["config"] = config
        return object()

    monkeypatch.setattr(bedrock, "_get_bedrock_client", _fake_get_client)

    bedrock._get_image_client()

    assert seen["config"].read_timeout >= 300
