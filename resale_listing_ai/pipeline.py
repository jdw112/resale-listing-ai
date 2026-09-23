"""The stage pipeline + four-state sufficiency gate + orchestrator.

Flow (one physical unit per submission):
  intake -> identify -> [ GATE ] -> product upsert -> listing -> pricing
         -> images -> copy -> publish

The gate runs right after identification and before any expensive pricing/image
work, so nothing costly happens on evidence that cannot support a result."""

import re
import sys

from . import adapters, admin_config
from .envelope import E, THRESHOLD, confident
from .helpers import make_sku, oversized_flag
from .records import blank_listing, blank_product

# Mandatory identity for a completed listing (PRD: title + category are mandatory;
# we also require a confident product identity anchor).
MANDATORY = ["Brand", "ProductName", "InternalCategory"]

# The core baseline this pipeline currently has real adapter coverage for, and
# that a genuinely well-evidenced item should be able to fill -- checked for
# "no unknowns" (a bare Accepted, not Accepted-with-unknowns). Deliberately
# NOT every schema field: packaged/unpackaged measurements have no intake
# path, ManualURL-and-friends have no adapter yet (gap #5), and the four
# optional copy fields beyond MasterTitle are allowed to stay blank by the
# copy prompt's own design.
# ModelNumber/UPCOrEAN aren't listed separately -- gate()'s has_anchor check
# below already requires one of them confident to reach this stage at all.
# Six of these fields live on the Product record, which is only (re)computed
# for a NEW product (see the `reused` branch below) -- a reused/deduped
# product inherits whatever CORE_FIELDS gaps its first unit left, with no
# retry path, so every later unit of that product reports the same
# "Accepted with unknowns" for a reason that isn't about that unit at all.
CORE_FIELDS = MANDATORY + [
    "PricingAnchorPriceCAD", "MasterTitle", "HeroImageURL", "AISuggestedConditionGrade",
    "ActualIncludedItems", "OriginalPackagingIncluded", "MissingHardware", "DefectsOrDamage",
]


# gate()'s missing-list mixes real identity field names (e.g. "Brand") with one
# synthetic display label ("ModelNumber/UPC", covering the has_anchor check) --
# a field_overrides key equal to that label must resolve to the real field it
# stands in for, or a submitter's answer to exactly the field the UI showed them
# would never reach identity at all.
_OVERRIDE_ALIASES = {"ModelNumber/UPC": "ModelNumber"}


def answerable_fields(missing):
    """The set of field_overrides keys a client may legitimately submit for a
    given gate() missing-list -- the literal label plus its aliased real name,
    in both directions, so either "ModelNumber/UPC" or "ModelNumber" is accepted."""
    fields = set(missing)
    for label, real in _OVERRIDE_ALIASES.items():
        if label in missing:
            fields.add(real)
        if real in missing:
            fields.add(label)
    return fields


def _key(identity):
    parts = [identity.get(f, {}).get("value") for f in ("Brand", "ModelNumber", "Colour")]
    parts = [re.sub(r"[^A-Za-z0-9]+", "", str(p)).upper() for p in parts if p]
    return "-".join(parts) or "UNKNOWN"


def gate(identity, config=None):
    """Four-state outcome. Returns (status, missing_fields)."""
    threshold = admin_config.get(config, "gate.threshold", THRESHOLD)
    mandatory = admin_config.get(config, "gate.mandatory_fields", MANDATORY)
    missing = [f for f in mandatory if not confident(identity.get(f), threshold)]
    has_anchor = (confident(identity.get("UPCOrEAN"), threshold)
                  or confident(identity.get("ModelNumber"), threshold))
    if not has_anchor:
        missing.append("ModelNumber/UPC")
    if missing:
        # a single clear miss is usually resolvable -> clarification; else reject
        status = "Clarification required" if len(missing) <= 2 else "Rejected"
        return status, missing
    return "Accepted", []


def _has_no_unknowns(product, listing, config=None):
    """True iff every core-fields entry is confident -- the item is genuinely
    complete, not just past the mandatory-identity gate."""
    threshold = admin_config.get(config, "gate.threshold", THRESHOLD)
    core_fields = admin_config.get(config, "gate.core_fields", CORE_FIELDS)
    for field in core_fields:
        record = product if field in product else listing
        if not confident(record.get(field, {}), threshold):
            return False
    return True


def _flush_cost_rows(store, ledger, sid):
    store.record_cost_rows([r for r in ledger.rows if r["submission_id"] == sid])


# Operational defaults for Listing fields nothing observes from photos: no testing
# stage runs here, and "nothing observed missing" is recorded as "None noted" rather
# than left null. Applied only where the field is still blank after unit_observe, so
# a real observation always wins. source "derived" (an operational default, not a
# claimed observation).
_LISTING_DEFAULTS = {
    "TestingStatus": "Not Tested",
    "TestsPerformed": "None",
    "TestResults": "Not Tested",
    "MissingItems": "None noted",
    "MissingComponents": "None noted",
    "MissingHardware": "None noted",
    "FunctionalConcerns": "None noted",
}


def _stage_ingest(submission, store, ledger, skus, config=None):
    """identify -> GATE -> product upsert -> listing build (no images yet).

    Returns (terminal, payload):
      terminal=True  -> payload is the final Rejected/Clarification-required result
      terminal=False -> payload is {"product_key", "sku", "reused"}, ready for
                         _stage_images (the queue/worker runtime enqueues an
                         image-queue job with exactly this; run() below just calls
                         straight through for the synchronous, single-process case).
    """
    sid = submission["submission_id"]
    if config is None:
        config = admin_config.load_overrides(store)

    # ---- identify (OCR + barcode + vision + measures) ----
    # ocr_read and barcode_read are both deterministic, local, ~free
    # pre-passes that run before vision_identify: OCR feeds it label-text
    # context (never fills a field on its own — the model still owns every
    # value/confidence/source), barcode fills UPCOrEAN directly when confident.
    ocr_text = adapters.ocr_read(submission)
    ledger.add(sid, "identify", "local_ocr", 0.000)  # deterministic, ~free
    # vision_identify self-logs its own cost row (stub: the historical flat
    # ("vision", $0.030); real: a provider-specific service tag and that
    # provider's real unit cost), so the identify stage is no longer a flat,
    # provider-blind number in the ledger.
    identity = adapters.vision_identify(submission, ledger=ledger, ocr_text=ocr_text, config=config)
    upc = adapters.barcode_read(submission)
    ledger.add(sid, "identify", "barcode", 0.000)  # deterministic, ~free
    if confident(upc):
        identity["UPCOrEAN"] = upc

    # ---- clarification-answer overrides (resale_listing_ai/api.py's POST
    # /v1/me/submissions/{id}/answer) -- a human explicitly typed these
    # values, trusted verbatim, same precedent as ProvidedConditionGrade
    # below, not re-verified through another model call.
    for field, value in submission.get("field_overrides", {}).items():
        identity[_OVERRIDE_ALIASES.get(field, field)] = E(value, 1.0, "provided")

    # ---- GATE (before pricing/images) ----
    status, missing = gate(identity, config)
    if status in ("Clarification required", "Rejected"):
        result = {
            "submission_id": sid, "status": status, "product_key": None, "listing_sku": None,
            "missing": missing,
            "reason": ("This is the criteria I wasn't able to extrapolate: "
                       + ", ".join(missing) + ". Provide more evidence and try again."),
            "cost_cad": ledger.total(sid),
        }
        return True, result

    # ---- product upsert (dedupe by ProductKey, via Postgres/store) ----
    product_key = _key(identity)
    existing_product = store.get_product(product_key)
    reused = existing_product is not None
    if reused:
        product = existing_product
    else:
        product = blank_product()
        product["ProductKey"] = E(product_key, 1.0, "derived")
        for f, env in identity.items():
            if f in product:
                product[f] = env
        for f, env in adapters.measures_lookup(
                submission, identity=identity, ledger=ledger, config=config).items():
            product[f] = env
        product["OversizedFlag"] = E(oversized_flag(
            product["PackagedLengthCM"]["value"], product["PackagedWidthCM"]["value"],
            product["PackagedHeightCM"]["value"], product["PackagedWeightKG"]["value"],
            config=config), 1.0, "derived")
        # pricing (expensive) — only for a NEW product, against the CONFIRMED
        # identity (not identity=None — a real pricing search needs the real
        # Brand/ProductName/Model to be meaningful, PRD §11.2). Both functions
        # self-log their own cost (stub or real) via the ledger param.
        for f, env in adapters.retail_price(submission, identity, ledger=ledger, config=config).items():
            product[f] = env
        for f, env in adapters.resale_price(submission, identity, ledger=ledger, config=config).items():
            product[f] = env
        for f, env in adapters.generate_copy(product, ledger=ledger, submission_id=sid, config=config).items():
            product[f] = env
        store.upsert_product(product)

    # ---- listing (this physical unit) ----
    listing = blank_listing()
    listing["ProductKey"] = E(product_key, 1.0, "derived")
    sku = make_sku(skus)
    skus.add(sku)
    listing["SKU"] = E(sku, 1.0, "generated")
    listing["ProvidedConditionGrade"] = E(submission["provided_condition_grade"], 1.0, "provided")
    for f, env in adapters.unit_observe(submission, ledger=ledger, config=config).items():
        if f in listing:
            listing[f] = env

    for f, default in _LISTING_DEFAULTS.items():
        if f in listing and listing[f].get("value") in (None, ""):
            listing[f] = E(default, 1.0, "derived")

    store.upsert_listing(listing)

    return False, {"product_key": product_key, "sku": sku, "reused": reused}


def _stage_images(submission, product_key, sku, reused, store, ledger, config=None):
    """images stage -> finalize listing + product; the terminal Accepted result."""
    sid = submission["submission_id"]
    if config is None:
        config = admin_config.load_overrides(store)
    product = store.get_product(product_key)
    listing = store.get_listing(sku)

    existing_hero = product["HeroImageURL"]
    imgs = adapters.image_process(
        submission, product_key, ledger=ledger,
        identity=product, already_has_hero=confident(existing_hero),
        already_has_website_hero=confident(product["WebsiteHeroImageURL"]), config=config,
    )

    # WebsiteHeroReviewRequired (spec §7) is a persisted, queryable signal -- set
    # here, before the upsert below, so it always reaches the DB even on a
    # submission that produces neither a confident hero nor a confident website
    # hero (e.g. no confident actual-item photo AND a failed AI generation).
    product["WebsiteHeroReviewRequired"] = E(
        "Yes" if imgs.get("website_hero_review") else "No", 1.0, "derived")

    # WebsiteHeroRejectedURL points the admin review link at the QC-rejected hero
    # image saved to storage. Written only when a rejected image was actually
    # saved (review flagged AND generation got far enough to produce one), so a
    # no-photo submission that never generated anything leaves it blank.
    rejected_url = imgs.get("website_hero_rejected")
    if rejected_url:
        product["WebsiteHeroRejectedURL"] = E(rejected_url, 1.0, "derived")

    # HeroImageURL is the clearest scrubbed actual-item photo (marketplace main
    # image). It is written only when a confident actual photo is available, is
    # superseded by a later, clearer actual, and is never blanked by a submission
    # that produced none. Tier-2 manufacturer lookup is retired.
    new_hero = imgs["hero"]
    if confident(new_hero):
        product["HeroImageURL"] = new_hero

    # WebsiteHeroImageURL is the AI-generated unbranded hero: generated once and
    # persisted when it passes QC (confident), never overwritten thereafter.
    website_hero = imgs["website_hero"]
    if not confident(product["WebsiteHeroImageURL"]) and confident(website_hero):
        product["WebsiteHeroImageURL"] = website_hero

    # WebsiteHeroPrompt records the exact generation prompt for audit/tuning --
    # persisted whenever one was built (kept or rejected hero), and on by default;
    # a customer can disable it (RESALE_LISTING_AI_PERSIST_HERO_PROMPT / admin override).
    prompt = imgs.get("website_hero_prompt")
    if prompt and adapters._persist_hero_prompt_enabled(config):
        product["WebsiteHeroPrompt"] = E(prompt, 1.0, "derived")

    # One upsert covers all of the above -- always run so
    # WebsiteHeroReviewRequired persists even when neither hero field changed.
    store.upsert_product(product)

    if imgs.get("website_hero_review"):
        print(f"[images] website hero flagged for manual review: {product_key}",
              file=sys.stderr, flush=True)

    listing["OriginalPhotoFolderURL"] = imgs["original_folder"]
    listing["ScrubbedPhotoFolderURL"] = imgs["scrubbed_folder"]
    for i, s in enumerate(imgs["supporting"], start=1):
        listing[f"ListingImageURL{i}"] = s
    for flag_name, env in imgs["flags"].items():
        listing[flag_name] = env

    store.upsert_listing(listing)

    final_status = "Accepted" if _has_no_unknowns(product, listing, config) else "Accepted with unknowns"

    return {
        "submission_id": sid, "status": final_status, "product_key": product_key,
        "listing_sku": sku, "reused_product": reused,
        "product": product, "listing": listing, "missing": [],
        "cost_cad": ledger.total(sid),
    }


def run(submission, store, ledger, skus=None, config=None):
    """Process one submission synchronously, both stages in one call. `store`
    (resale_listing_ai/db.py) is the source of truth: it dedupes Product rows by
    ProductKey (ON CONFLICT DO UPDATE) and is the idempotency key for the whole
    submission — redelivery/reprocessing of the same submission_id is a safe
    no-op, checked before any stage runs. `skus` is a shared set so generated SKUs
    stay unique within this process's lifetime.

    The queue/worker runtime (resale_listing_ai/worker.py) calls _stage_ingest and
    _stage_images separately, across the ingest-queue and image-queue, with a
    per-stage idempotency checkpoint (store.get_submission_state) between them;
    this function is the synchronous composition of the two for direct callers
    (tests, run_demo.py) that don't need the queue in between."""
    sid = submission["submission_id"]
    if skus is None:
        skus = set()

    cached = store.get_submission_result(sid)
    if cached is not None:
        return cached  # redelivery/reprocessing -> no stage re-runs, nothing duplicated

    terminal, payload = _stage_ingest(submission, store, ledger, skus, config=config)
    if terminal:
        _flush_cost_rows(store, ledger, sid)
        store.record_submission(sid, payload["status"], payload)
        return payload

    result = _stage_images(submission, payload["product_key"], payload["sku"], payload["reused"],
                            store, ledger, config=config)
    _flush_cost_rows(store, ledger, sid)
    store.record_submission(sid, result["status"], result)
    return result
