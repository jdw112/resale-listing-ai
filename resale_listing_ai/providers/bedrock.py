"""Bedrock adapter — a comparison provider for the vision/classify seams
(resale_listing_ai/adapters.py dispatches here when RESALE_LISTING_AI_VISION_PROVIDER selects
"bedrock") and image_process (Nova Canvas background removal + mask-based
inpaint, when RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER selects "bedrock"). Uses boto3 (already a dependency for S3/SQS) — no new
package."""

import json
import os
from base64 import b64decode, b64encode
from pathlib import Path

BEDROCK_REGION = os.environ.get("RESALE_LISTING_AI_BEDROCK_REGION", "us-east-1")
# Claude 4.x models cannot be invoked on Bedrock by their BASE model id with
# on-demand throughput -- that returns "ValidationException: Invocation of model
# ID ... with on-demand throughput isn't supported. Retry your request with the
# ID or ARN of an inference profile". The working id is the region-prefixed
# CROSS-REGION INFERENCE PROFILE, and the prefix must match the region in
# RESALE_LISTING_AI_BEDROCK_REGION: "us." for us-*, "eu." for eu-*, "apac." for ap-*,
# "global." for the global profile. The default below pairs with this module's
# default region (us-east-1) -- change BOTH together.
BEDROCK_MODEL = os.environ.get("RESALE_LISTING_AI_BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_BEDROCK_UNIT_COST_CAD", "0.02"))


def _get_bedrock_client(config=None):
    """Two supported auth paths, checked in this order:

    1. AWS_BEARER_TOKEN_BEDROCK -- the account's Bedrock API key (a bearer
       token, distinct from a full IAM access key). botocore picks this up
       itself for Bedrock services once the env var is set -- no explicit
       credential object needed here.
    2. Standard AWS credentials (env vars or IAM role) -- the same boto3
       credential chain already relied on for S3/SQS, used as a fallback so
       an account already using full IAM creds for Bedrock isn't broken.

    `config` is an optional botocore.config.Config (same optional-config shape
    as adapters._get_s3_client) -- the Nova Canvas image calls use it to raise
    boto3's 60s default read timeout."""
    import boto3

    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        session = boto3.Session()
        if session.get_credentials() is None:
            raise RuntimeError(
                "No Bedrock credentials found. Set AWS_BEARER_TOKEN_BEDROCK (a "
                "Bedrock API key) or standard AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
                "credentials (or configure an IAM role) for real Bedrock calls, "
                "switch RESALE_LISTING_AI_VISION_PROVIDER/RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER "
                "to a different provider, or set RESALE_LISTING_AI_STUBS=1 to run the "
                "offline stub adapters."
            )
    return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=config)


# The ONLY image formats Bedrock's Converse API accepts, keyed by the format
# name Pillow reports (Image.format). Anything else — most importantly HEIC, the
# default camera format on the iPads/iPhones submitters actually photograph with
# — is re-encoded to JPEG here, because AWS would reject it outright. The
# filename extension is deliberately NOT trusted: it is the file's real decoded
# content that has to match what the `format` field claims.
_BEDROCK_ACCEPTED_FORMATS = {"PNG": "png", "JPEG": "jpeg", "GIF": "gif", "WEBP": "webp"}

_JPEG_QUALITY = 90  # photo-quality default for the convert-to-JPEG path

_heif_registered = False


def _register_heif_opener():
    """Teach Pillow to open .heic/.heif once per process. pillow-heif is a
    Pillow plugin: after register_heif_opener() a HEIC file opens through the
    normal PIL.Image.open path like any other format. Lazy (not module-level)
    to match this file's existing convention of importing heavy/optional deps
    inside the functions that need them."""
    global _heif_registered
    if _heif_registered:
        return
    try:
        import pillow_heif
    except ImportError:  # pragma: no cover - pillow-heif is in requirements.txt
        # Leave HEIC unsupported rather than failing every other format: an
        # actual .heic input then fails loudly in _image_block below, naming
        # the missing package, instead of being silently sent as garbage.
        _heif_registered = True
        return
    pillow_heif.register_heif_opener()
    _heif_registered = True


def _jpeg_bytes(img):
    from io import BytesIO

    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


def _image_block(image_path):
    """One Converse image content block. Bedrock accepts only png/jpeg/gif/webp,
    so the file's REAL format is decoded (including HEIC via pillow-heif) and
    anything outside that set is converted to JPEG before it leaves this
    machine. Already-accepted formats are passed through byte-identical — no
    pointless re-compression of the common JPEG/PNG case."""
    from io import BytesIO

    from PIL import Image as PILImage

    _register_heif_opener()
    data = Path(image_path).read_bytes()
    try:
        img = PILImage.open(BytesIO(data))
        fmt = _BEDROCK_ACCEPTED_FORMATS.get((img.format or "").upper())
        if fmt is not None:
            return {"image": {"format": fmt, "source": {"bytes": data}}}
        converted = _jpeg_bytes(img)
    except (OSError, ValueError) as exc:
        # PIL.UnidentifiedImageError subclasses OSError. Never guess a format
        # for an undecodable file (that would send garbage to a billed API and
        # get a confusing ValidationException back) — fail with the real cause.
        raise ValueError(
            f"Cannot decode image {image_path} for Bedrock ({exc}). Bedrock accepts "
            "png/jpeg/gif/webp; other formats are converted here first, which needs "
            "Pillow (plus pillow-heif for iPhone/iPad .heic photos)."
        ) from exc
    return {"image": {"format": "jpeg", "source": {"bytes": converted}}}


def _converse_text(client, content):
    response = client.converse(modelId=BEDROCK_MODEL, messages=[{"role": "user", "content": content}])
    return response["output"]["message"]["content"][0]["text"]


def vision_json(instructions, notes, images):
    """Product-identity extraction from photos. Returns raw response text for
    the caller to run through resale_listing_ai.json_repair.repair_json — never
    trusted directly."""
    client = _get_bedrock_client()
    content = [{"text": instructions}]
    if notes:
        content.append({"text": f"Submitter notes: {notes}"})
    content.extend(_image_block(path) for path in images)
    return _converse_text(client, content)


def classify_json(instructions, image):
    """Single-image classification (photo category / cleanup flag)."""
    client = _get_bedrock_client()
    return _converse_text(client, [{"text": instructions}, _image_block(image)])


BEDROCK_IMAGE_MODEL = os.environ.get("RESALE_LISTING_AI_BEDROCK_IMAGE_MODEL", "amazon.nova-canvas-v1:0")
BEDROCK_IMAGE_UNIT_COST_CAD = float(os.environ.get("RESALE_LISTING_AI_BEDROCK_IMAGE_UNIT_COST_CAD", "0.02"))

# Nova Canvas input-image limits (AWS "Amazon Nova Canvas image generation
# access and usage"): every side 320-4096 px, aspect ratio between 1:4 and 4:1,
# and FEWER than 4,194,304 total pixels. A stock phone photo (4032x3024 =
# 12.2 MP) fails the pixel cap outright, so submitter photos are downscaled to
# fit before every call rather than being rejected at the API.
NOVA_CANVAS_MAX_PIXELS = 4_194_304
NOVA_CANVAS_MAX_SIDE = 4096
NOVA_CANVAS_MIN_SIDE = 320

# boto3's default read timeout is 60s; AWS recommends >=300s for Nova Canvas
# generation calls. Without it a slow-but-succeeding generation surfaces as a
# retryable ClientError and gets billed on every retry.
BEDROCK_IMAGE_READ_TIMEOUT = int(os.environ.get("RESALE_LISTING_AI_BEDROCK_IMAGE_READ_TIMEOUT", "300"))


def _get_image_client():
    """bedrock-runtime client for the Nova Canvas invoke_model calls, with the
    longer read timeout. The Converse/vision path deliberately does NOT use
    this -- text generation returns well inside the default timeout."""
    from botocore.config import Config

    return _get_bedrock_client(config=Config(read_timeout=BEDROCK_IMAGE_READ_TIMEOUT))


def _png_bytes(img):
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _nova_canvas_target_size(width, height):
    """Target (width, height) that fits Nova Canvas's input limits, preserving
    aspect ratio — or None when the image already fits and must be sent
    untouched. Only ever scales DOWN: upscaling would invent pixels."""
    if width * height < NOVA_CANVAS_MAX_PIXELS and max(width, height) <= NOVA_CANVAS_MAX_SIDE:
        return None
    scale = min(
        # MAX_PIXELS - 1: the limit is "fewer than" 4,194,304, so an image
        # sitting exactly on the cap still has to come down a notch.
        ((NOVA_CANVAS_MAX_PIXELS - 1) / float(width * height)) ** 0.5,
        NOVA_CANVAS_MAX_SIDE / float(width),
        NOVA_CANVAS_MAX_SIDE / float(height),
    )
    if scale >= 1.0:
        return None
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    if min(target) < NOVA_CANVAS_MIN_SIDE:
        # Downscaling would trade the pixel-cap rejection for a min-side one.
        # Unreachable inside Nova's own 1:4-4:1 aspect range (at the pixel cap
        # the short side is >= 1024 px), so this only guards images Nova would
        # reject on aspect ratio anyway: send as-is and let the API say no,
        # rather than silently cropping the submitter's photo.
        return None
    return target


def _fit_image_for_nova_canvas(img):
    """Downscale a PIL image to Nova Canvas's input limits, returning the SAME
    object (identity-comparable, so callers can tell "untouched" from "resized")
    when it already fits. The one place the resize happens — both the
    background-removal and the inpaint path go through here."""
    from PIL import Image as PILImage

    target = _nova_canvas_target_size(*img.size)
    if target is None:
        return img
    return img.resize(target, PILImage.LANCZOS)


def _fit_for_nova_canvas(image_bytes):
    """Downscale image_bytes to Nova Canvas's input limits if needed, returning
    the bytes UNCHANGED (not re-encoded) when the image already fits."""
    from io import BytesIO

    from PIL import Image as PILImage

    img = PILImage.open(BytesIO(image_bytes))
    fitted = _fit_image_for_nova_canvas(img)
    if fitted is img:
        return image_bytes
    return _png_bytes(fitted)


def _flatten_to_opaque(img):
    """Composite any alpha onto opaque white. Nova Canvas's BACKGROUND_REMOVAL
    returns "a PNG image with full 8-bit transparency", but an INPAINTING input
    image "must not contain any transparent or translucent pixels" — so the
    background-removed output cannot legally be fed straight to the inpaint."""
    from PIL import Image as PILImage

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = PILImage.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        return flat
    return img.convert("RGB")


def _prepare_inpaint_inputs(image_bytes, mask_bytes):
    """Convert the pipeline's shared (image, mask) pair into what Nova Canvas's
    INPAINTING task actually accepts:

    1. THE MASK IS INVERTED. adapters._build_cleanup_mask paints the clutter
       WHITE on a black field -- Clipdrop's convention (white = clean this),
       and it stays that way because the default photoroom_clipdrop path shares
       it. Nova Canvas is the exact opposite: "Areas to be edited are shaded
       pure black and areas to ignore are shaded pure white." Sent unchanged,
       Nova would PRESERVE the measuring tape and regenerate the entire rest of
       the frame -- the product itself.
    2. Alpha is flattened to opaque (see _flatten_to_opaque).
    3. Image and mask are resized TOGETHER when the image is over Nova's input
       limits, so the mask keeps covering exactly the same pixels. The mask is
       resized with NEAREST so it stays pure black/white -- an interpolated
       edge would be a grey (undefined) mask pixel.

    Returns (image_png_bytes, mask_png_bytes), both RGB."""
    from io import BytesIO

    from PIL import Image as PILImage
    from PIL import ImageOps

    img = _fit_image_for_nova_canvas(_flatten_to_opaque(PILImage.open(BytesIO(image_bytes))))
    mask = ImageOps.invert(PILImage.open(BytesIO(mask_bytes)).convert("L")).convert("RGB")

    if mask.size != img.size:
        mask = mask.resize(img.size, PILImage.NEAREST)
    return _png_bytes(img), _png_bytes(mask)


def _invoke_image_model(client, body):
    response = client.invoke_model(
        modelId=BEDROCK_IMAGE_MODEL,
        body=json.dumps(body).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    images = payload.get("images") or []
    if not images:
        raise ValueError("Bedrock Nova Canvas response contained no image")
    return b64decode(images[0])


def remove_background_image(image_bytes):
    """Background-remove a product photo via Nova Canvas. Returns raw image
    bytes for the caller to upload — never trusted directly, same posture as
    every other provider's raw-bytes return."""
    client = _get_image_client()
    fitted = _fit_for_nova_canvas(image_bytes)
    body = {
        "taskType": "BACKGROUND_REMOVAL",
        "backgroundRemovalParams": {"image": b64encode(fitted).decode("ascii")},
    }
    return _invoke_image_model(client, body)


def inpaint_region_image(image_bytes, mask_bytes):
    """Inpaint ONLY the masked region via Nova Canvas's explicit-mask
    INPAINTING task — the same hard mask-boundary safety property
    _clipdrop_cleanup already relies on (real defect evidence elsewhere in
    the frame is architecturally untouched), unlike Gemini's prompt-driven
    marker approach. See _prepare_inpaint_inputs for the mask-polarity
    inversion that makes that property actually hold on Nova Canvas.

    No `text` field is sent: AWS documents that omitting it makes the model
    "remove elements inside the masked area ... replaced with a seamless
    extension of the image background", which is exactly the wanted behaviour,
    and AWS warns against the negating phrasing ("no measuring tape, ruler,
    clutter") a hand-written prompt would need here."""
    client = _get_image_client()
    image, mask = _prepare_inpaint_inputs(image_bytes, mask_bytes)
    body = {
        "taskType": "INPAINTING",
        "inPaintingParams": {
            "image": b64encode(image).decode("ascii"),
            "maskImage": b64encode(mask).decode("ascii"),
        },
    }
    return _invoke_image_model(client, body)
