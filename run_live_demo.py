"""Live end-to-end demo: real provider calls for both the ingest stage
(vision/barcode/copy/web-search) and the images stage (background removal +
cleanup + hero/supporting image storage).

The images stage defaults to RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER=photoroom_clipdrop
(resale_listing_ai/adapters.py), which this repo has no API keys for -- set
RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER=gemini to use the Gemini image-editing branch
instead. Image storage auto-detects: it probes S3 (RESALE_LISTING_AI_S3_BUCKET) and
falls back to local disk automatically if unavailable, so no bucket/keys are
required for a local run.

Usage (run once per provider set, diff the printed output by hand):

  RESALE_LISTING_AI_VISION_PROVIDER=gemini RESALE_LISTING_AI_COPY_PROVIDER=gemini \\
  RESALE_LISTING_AI_WEB_SEARCH_PROVIDER=gemini_search \\
  RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER=gemini \\
  python3 run_live_demo.py uploads/photo1.jpg uploads/photo2.jpg

  RESALE_LISTING_AI_VISION_PROVIDER=openrouter RESALE_LISTING_AI_COPY_PROVIDER=openrouter \\
  RESALE_LISTING_AI_WEB_SEARCH_PROVIDER=openrouter_online \\
  RESALE_LISTING_AI_OPENROUTER_MODEL=google/gemini-2.0-flash-001 \\
  RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER=gemini \\
  python3 run_live_demo.py uploads/photo1.jpg uploads/photo2.jpg

Or point it at a per-product folder (matches how the PRD describes real
intake -- "a submission may contain two images or ten"; non-image files
like the "Notes.txt" notes file are skipped automatically):

  RESALE_LISTING_AI_IMAGE_PROCESS_PROVIDER=gemini python3 run_live_demo.py "uploads/Product 01"

RESALE_LISTING_AI_MAX_IMAGES=<n> optionally caps how many images are taken from a
directory (first n by filename). No cap by default -- pick one based on
the provider/model's own payload limit (e.g. OpenRouter's free tier has
rejected requests over ~30MB of combined image data in testing).

Needs GEMINI_API_KEY and/or OPENROUTER_API_KEY in .env (see .env.example);
OpenRouter also needs RESALE_LISTING_AI_OPENROUTER_MODEL set (no default -- its
catalog changes over time, see resale_listing_ai/providers/openrouter.py). Also
needs pyzbar + system zbar (`brew install zbar`) for barcode_read, and
pytesseract + system tesseract (`brew install tesseract`) for ocr_read, both
of which are provider-agnostic and always real unless stubbed — see the main
README.
"""

import os
import re
import sys
from pathlib import Path

os.environ.pop("RESALE_LISTING_AI_STUBS", None)  # this script is real-provider-only

from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store, _load_dotenv
from resale_listing_ai.envelope import confident
from resale_listing_ai.pipeline import _stage_images, _stage_ingest

_load_dotenv()  # before any os.environ.get() below -- Store.connect() also
                 # calls this, but too late for the RESALE_LISTING_AI_MAX_IMAGES read

UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tif", ".tiff"}


def _images_from_directory(directory):
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <image1> [image2 ...]")
        print(f"       python3 {sys.argv[0]} <directory>")
        print(f"Place real product photos under {UPLOADS_DIR}/ and pass their paths, "
              "or a folder of them.")
        print("Optional: RESALE_LISTING_AI_MAX_IMAGES=<n> caps how many images are taken from "
              "a directory (no cap by default -- depends on the provider/model in use).")
        raise SystemExit(1)

    # A common mistake: passing FOO=bar as a script argument instead of an
    # environment variable set before the command. Catch it with a specific
    # fix instead of the generic "Not a file" error a stray path-looking
    # argument like this would otherwise produce.
    env_like = [a for a in sys.argv[1:] if re.match(r"^[A-Z_][A-Z0-9_]*=", a)]
    if env_like:
        example = env_like[0]
        print(f"{example!r} looks like an environment variable, not a file or directory path.")
        print("Environment variables must go BEFORE the command on the same line, e.g.:")
        print(f"  {example} .venv/bin/python {sys.argv[0]} <image(s) or directory>")
        print("(not after it, as a script argument -- that's what just happened here).")
        raise SystemExit(1)

    arg_path = Path(sys.argv[1])
    if len(sys.argv) == 2 and arg_path.is_dir():
        found = _images_from_directory(arg_path)
        if not found:
            print(f"No images found in {arg_path}")
            raise SystemExit(1)
        max_images = os.environ.get("RESALE_LISTING_AI_MAX_IMAGES")
        if max_images is not None:
            found = found[:int(max_images)]
        images = [str(p) for p in found]
        print(f"Using {len(images)} image(s) from {arg_path}: {[p.name for p in found]}")
    else:
        images = sys.argv[1:]
        for image in images:
            if not Path(image).is_file():
                print(f"Not a file: {image}")
                raise SystemExit(1)

    UPLOADS_DIR.mkdir(exist_ok=True)

    submission = {
        "submission_id": "live-demo-0001",
        "images": images,
        "notes": None,
        "provided_condition_grade": "Used - Good",
    }

    store = Store.connect()
    store.ensure_schema()
    ledger = CostLedger()

    vision = os.environ.get("RESALE_LISTING_AI_VISION_PROVIDER", "openai")
    copy = os.environ.get("RESALE_LISTING_AI_COPY_PROVIDER", "openai")
    web_search = os.environ.get("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "openai_web_search")

    print("=" * 68)
    print(f"RESALE LISTING AI LIVE DEMO  vision={vision}  copy={copy}  web_search={web_search}")
    print("=" * 68)

    terminal, payload = _stage_ingest(submission, store, ledger, set())

    if terminal:
        print(f"Status: {payload['status']}")
        print(f"Missing: {payload['missing']}")
        print(f"Reason: {payload['reason']}")
        store.close()
        return

    result = _stage_images(
        submission, payload["product_key"], payload["sku"], payload["reused"], store, ledger,
    )

    product = result["product"]
    listing = result["listing"]

    print(f"Status: {result['status']}")
    print(f"ProductKey: {payload['product_key']}   SKU: {payload['sku']}   reused: {payload['reused']}")

    if payload["reused"]:
        # _stage_ingest dedupes by ProductKey (correct, core pipeline behavior —
        # one Product record per make/model/variant). But this script uses a
        # constant submission_id and the same photos across a Gemini pass and an
        # OpenRouter pass, so the second pass matches the first pass's
        # ProductKey: pricing and copy are NOT regenerated, and everything below
        # is the FIRST provider's stored output printed under the SECOND
        # provider's banner. Say so loudly rather than showing a fake comparison.
        print()
        print("!" * 68)
        print("WARNING: this submission matched an EXISTING product (ProductKey reused).")
        print("Pricing and copy below were NOT freshly generated by the current provider")
        print("set — they are the ORIGINAL product's stored values, so comparing this")
        print("report against another provider's run is meaningless. The cost ledger")
        print("below is also missing the pricing/copy calls that were skipped.")
        print("For a genuine comparison, clear the product store between provider")
        print("passes — e.g. delete/point RESALE_LISTING_AI_SQLITE_PATH at a fresh file, or")
        print("drop this ProductKey from the products table. (Note: changing")
        print("provided_condition_grade does NOT help — ProductKey is derived from")
        print("Brand/ModelNumber/Colour only, and condition lives on the Listing.)")
        print("!" * 68)

    print("\nIdentity fields:")
    for field in ("Brand", "ProductName", "ModelNumber", "Variant", "Colour", "InternalCategory"):
        env = product[field]
        print(f"  {field:<20} = {env['value']!r:<40} (confidence={env['confidence']}, source={env['source']})")

    print("\nPricing:")
    for field in ("PricingAnchorPriceCAD", "PricingAnchorSourceURL",
                   "ComparableProductPriceCAD", "ComparableProductURL"):
        print(f"  {field:<26} = {product[field]['value']}")

    print("\nGenerated copy:")
    for field in ("MasterTitle", "ShortTitle", "MasterDescription"):
        print(f"  {field:<20} = {product[field]['value']}")

    print("\nListing (this physical unit):")
    print(f"  ProvidedConditionGrade = {listing['ProvidedConditionGrade']['value']}")

    print("\nImages:")
    print(f"  HeroImageURL           = {product['HeroImageURL']['value']} (source={product['HeroImageURL']['source']})")
    print(f"  OriginalPhotoFolderURL = {listing['OriginalPhotoFolderURL']['value']}")
    print(f"  ScrubbedPhotoFolderURL = {listing['ScrubbedPhotoFolderURL']['value']}")
    for i in range(1, 10):
        field = f"ListingImageURL{i}"
        if field not in listing or not confident(listing[field]):
            break
        print(f"  {field:<23} = {listing[field]['value']}")

    print("\nCost ledger for this submission:")
    for row in ledger.rows:
        toks = ""
        if row.get("input_tokens") is not None or row.get("output_tokens") is not None:
            toks = (f"  tokens={row.get('input_tokens') or 0}in/"
                    f"{row.get('output_tokens') or 0}out  model={row.get('model')}")
        print(f"  {row['stage']:<10} {row['service']:<24} ${row['cost_cad']:.4f}  "
              f"retry={row['retry']}{toks}")
    print(f"  TOTAL: ${ledger.total(submission['submission_id']):.4f}")

    store.close()


if __name__ == "__main__":
    main()
