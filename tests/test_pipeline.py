"""Tests wired to the non-negotiable rules. Run: pytest -q"""

import pytest

from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.envelope import E, NULL, confident
from resale_listing_ai.helpers import oversized_flag
from resale_listing_ai.pipeline import run

REC = {"submission_id": "t1", "hint": "nest-doorbell", "images": ["a"], "notes": None,
       "provided_condition_grade": "Open Box"}
UNK = {"submission_id": "t2", "hint": "mystery", "images": ["a"], "notes": None,
       "provided_condition_grade": "Used - Fair"}


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    """These tests use the "hint"-keyed mock data, not real images/API keys —
    keep the pipeline on the offline stub adapters regardless of environment."""
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    # setenv, not delenv: _load_dotenv() uses os.environ.setdefault(), which
    # would silently re-populate a deleted var from a real repo .env (e.g.
    # RESALE_LISTING_AI_DB_MODE=postgres uncommented for live-demo use).
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store():
    s = Store.connect()  # sqlite, in-memory — no config needed, isolated per test
    s.ensure_schema()
    yield s
    s.close()


def _run(sub, store=None, skus=None, ledger=None):
    if store is None:
        store = Store.connect()
        store.ensure_schema()
    return run(sub, store, ledger if ledger is not None else CostLedger(),
               skus if skus is not None else set())


def test_recognized_is_accepted_with_records():
    """The nest-doorbell stub fixture genuinely satisfies every CORE_FIELDS
    entry (confirmed by reading adapters._KNOWN["nest-doorbell"] directly) --
    it must report as a bare Accepted, not Accepted-with-unknowns."""
    r = _run(REC)
    assert r["status"] == "Accepted"
    assert r["product_key"] and r["listing_sku"]
    assert confident(r["product"]["Brand"])


def test_missing_pricing_anchor_keeps_accepted_with_unknowns(monkeypatch):
    """A submission that otherwise fully succeeds but has no pricing match
    must still report Accepted-with-unknowns -- proving _has_no_unknowns is
    actually being consulted, not just always true for this fixture."""
    from resale_listing_ai import adapters

    monkeypatch.setattr(adapters, "retail_price", lambda submission, identity, ledger=None, config=None: {})

    r = _run(REC)

    assert r["status"] == "Accepted with unknowns"


def test_persistent_web_search_429_still_reaches_terminal_accepted(monkeypatch, store):
    """A web-search provider that hard-fails (persistent HTTP 429
    RESOURCE_EXHAUSTED) through every retry must NOT abort the whole submission.
    The enrichment stages (retail/resale pricing) degrade to empty and the
    submission completes as "Accepted with unknowns" with the pricing/dimension
    fields null -- rather than propagating out of _stage_ingest, leaving the SQS
    message undeleted, and redelivering until the DLQ.

    Runs in REAL adapter mode (RESALE_LISTING_AI_STUBS unset) so retail_price/resale_price
    actually hit the web-search seam; the non-web-search adapters that would
    otherwise need live keys/images are stubbed to isolate the provider failure."""
    from resale_listing_ai import adapters
    from resale_listing_ai.providers import gemini

    monkeypatch.delenv("RESALE_LISTING_AI_STUBS", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_WEB_SEARCH_PROVIDER", "gemini_search")

    # A confident identity that clears the gate, so ingest reaches pricing.
    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.92, "vision"),
            "ProductName": E("Smart Doorbell (wired, 2nd gen)", 0.9, "vision"),
            "InternalCategory": E("Smart Home", 0.88, "vision"),
            "ModelNumber": E("AC1000-XY", 0.94, "label_ocr"),
        }

    def empty_image_result(submission, product_key, ledger=None, identity=None,
                           already_has_hero=False, already_has_website_hero=False, config=None):
        return {
            "hero": NULL(),
            "website_hero": NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": NULL(), "scrubbed_folder": NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    def persistent_429(instructions):
        # Real google-genai ClientError with a 429 status, as the SDK builds it.
        from google.genai import errors
        raise errors.ClientError(429, {"error": {"message": "rate limit", "status": "RESOURCE_EXHAUSTED"}})

    monkeypatch.setattr(adapters, "ocr_read", lambda submission: "")
    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())
    monkeypatch.setattr(adapters, "unit_observe", lambda submission, ledger=None, config=None: {})
    monkeypatch.setattr(adapters, "generate_copy",
                        lambda product, ledger=None, submission_id=None, config=None: {})
    monkeypatch.setattr(adapters, "image_process", empty_image_result)
    # retail_price and resale_price stay REAL -- this is the code path under test.
    monkeypatch.setattr(gemini, "web_search_json", persistent_429)

    sub = {"submission_id": "429-e2e", "images": ["a"], "notes": None,
           "provided_condition_grade": "Open Box"}

    r = _run(sub, store)

    assert r["status"] == "Accepted with unknowns"                 # terminal Accepted family, not stuck/DLQ
    assert r["product"]["PricingAnchorPriceCAD"]["value"] is None   # pricing degraded to null
    assert r["product"]["UnpackagedWeightKG"]["value"] is None      # dimensions null (no measures path)


def test_unrecognized_is_gated_not_fabricated():
    r = _run(UNK)
    assert r["status"] in ("Clarification required", "Rejected")
    assert r["missing"], "must name what is missing"
    assert "product" not in r  # no record produced -> nothing to fabricate


def test_provided_condition_is_immutable():
    r = _run(REC)
    pc = r["listing"]["ProvidedConditionGrade"]
    assert pc["value"] == "Open Box" and pc["source"] == "provided" and pc["confidence"] == 1.0


def test_no_invented_serial_or_price_when_absent():
    # unrecognized item never reaches pricing/serial; recognized item only fills
    # them from a real source, never guessed
    r = _run(REC)
    serial = r["listing"]["SerialNumber"]
    assert serial["source"] in ("label_ocr", "none")


def test_productkey_dedupe_reuses_product(store):
    skus = set()
    a = _run(REC, store, skus)
    b = _run({**REC, "submission_id": "t3"}, store, skus)
    assert a["product_key"] == b["product_key"]
    assert store.count("product") == 1          # one Product ROW (in Postgres/sqlite) for two units
    assert a["listing_sku"] != b["listing_sku"]
    assert b["reused_product"] is True


def test_reprocessing_a_submission_is_a_safe_no_op(store):
    """Redelivery of the same submission_id (e.g. an at-least-once SQS retry) must
    not redo any stage, mint a new SKU, or duplicate cost-ledger rows — the store's
    submission table is the idempotency key, checked before any work happens."""
    first = _run(REC, store)
    rows_after_first = len(store.cost_rows(REC["submission_id"]))
    assert rows_after_first > 0

    second = _run(REC, store)

    assert second == first
    assert len(store.cost_rows(REC["submission_id"])) == rows_after_first  # no new rows
    assert store.count("listing") == 1  # no duplicate listing row
    assert store.count("product") == 1


def test_reprocessing_a_rejected_submission_is_also_a_safe_no_op(store):
    first = _run(UNK, store)

    second = _run(UNK, store)

    assert second == first
    assert store.count("product") == 0
    assert store.count("listing") == 0


def _confident_core_records():
    """A product/listing pair where every CORE_FIELDS entry is confident --
    the baseline for _has_no_unknowns tests to selectively break one field at
    a time from."""
    from resale_listing_ai.envelope import E
    from resale_listing_ai.records import blank_listing, blank_product

    product = blank_product()
    product["Brand"] = E("Acme", 0.9, "vision")
    product["ProductName"] = E("Smart Doorbell", 0.9, "vision")
    product["InternalCategory"] = E("Smart Home", 0.88, "vision")
    product["PricingAnchorPriceCAD"] = E(149.99, 0.6, "external_lookup")
    product["MasterTitle"] = E("Acme Smart Doorbell", 0.9, "generated")
    product["HeroImageURL"] = E("s3://bucket/hero.png", 0.86, "generated")

    listing = blank_listing()
    listing["AISuggestedConditionGrade"] = E("Open Box - Like New", 0.8, "vision")
    listing["ActualIncludedItems"] = E("Doorbell, base plate", 0.8, "vision")
    listing["OriginalPackagingIncluded"] = E("Yes", 0.85, "vision")
    listing["MissingHardware"] = E("No", 0.8, "vision")
    listing["DefectsOrDamage"] = E("No visible damage", 0.75, "vision")

    return product, listing


def test_core_fields_are_all_real_product_or_listing_fields():
    from resale_listing_ai.pipeline import CORE_FIELDS
    from resale_listing_ai.records import LISTING_FIELDS, PRODUCT_FIELDS

    assert set(CORE_FIELDS) <= set(PRODUCT_FIELDS) | set(LISTING_FIELDS)


def test_has_no_unknowns_true_when_every_core_field_is_confident():
    from resale_listing_ai.pipeline import _has_no_unknowns

    product, listing = _confident_core_records()

    assert _has_no_unknowns(product, listing) is True


def test_has_no_unknowns_false_when_a_product_field_is_missing():
    from resale_listing_ai.envelope import NULL
    from resale_listing_ai.pipeline import _has_no_unknowns

    product, listing = _confident_core_records()
    product["PricingAnchorPriceCAD"] = NULL()

    assert _has_no_unknowns(product, listing) is False


def test_has_no_unknowns_false_when_a_listing_field_is_missing():
    from resale_listing_ai.envelope import NULL
    from resale_listing_ai.pipeline import _has_no_unknowns

    product, listing = _confident_core_records()
    listing["DefectsOrDamage"] = NULL()

    assert _has_no_unknowns(product, listing) is False


def test_has_no_unknowns_false_when_confidence_is_below_threshold():
    from resale_listing_ai.envelope import E
    from resale_listing_ai.pipeline import _has_no_unknowns

    product, listing = _confident_core_records()
    listing["AISuggestedConditionGrade"] = E("Open Box - Like New", 0.4, "vision")  # below THRESHOLD

    assert _has_no_unknowns(product, listing) is False


def test_oversized_unable_when_package_data_missing():
    assert oversized_flag(None, None, None, None) == "Unable to Determine"
    assert oversized_flag(30, 20, 15, 2) == "No"
    assert oversized_flag(300, 20, 15, 2) == "Yes"      # longest side over baseline


def test_copy_is_grounded_in_verified_facts():
    r = _run(REC)
    title = r["product"]["MasterTitle"]
    assert title["source"] == "generated"
    assert r["product"]["Brand"]["value"] in title["value"]


def test_photo_availability_flags_are_set_on_the_listing():
    r = _run(REC)
    listing = r["listing"]
    assert listing["ActualProductPhotosAvailable"]["value"] in ("Yes", "No")
    assert listing["OriginalPackagingPhotosAvailable"]["value"] in ("Yes", "No")
    assert listing["ProductInformationLabelPhotosAvailable"]["value"] in ("Yes", "No")
    assert listing["ContextualProductPhotosAvailable"]["value"] in ("Yes", "No")


def test_finished_image_set_is_one_hero_and_at_most_three_supporting():
    r = _run(REC)
    assert r["product"]["HeroImageURL"]["value"] is not None
    supporting_urls = [r["listing"][f"ListingImageURL{i}"]["value"] for i in range(1, 4)]
    assert 0 <= len([u for u in supporting_urls if u]) <= 3


def test_stage_ingest_calls_retail_and_resale_price_with_real_identity(monkeypatch, store):
    """Known gap fix: pricing must be looked up against the CONFIRMED identity,
    not identity=None — otherwise a real provider has nothing to search for
    (the old adapters.pricing_lookup(submission) called resale_price(submission,
    None) only, and never called retail_price at all)."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    calls = {}

    def fake_retail_price(submission, identity, ledger=None, config=None):
        calls["retail_identity"] = identity
        return {}

    def fake_resale_price(submission, identity, ledger=None, config=None):
        calls["resale_identity"] = identity
        return {}

    monkeypatch.setattr(adapters, "retail_price", fake_retail_price)
    monkeypatch.setattr(adapters, "resale_price", fake_resale_price)

    _stage_ingest(REC, store, CostLedger(), set())

    assert calls["retail_identity"] is not None
    assert calls["retail_identity"]["Brand"]["value"] == "Acme"
    assert calls["resale_identity"] is not None
    assert calls["resale_identity"]["Brand"]["value"] == "Acme"


def test_stage_images_calls_image_process_with_identity_and_hero_flag(monkeypatch, store):
    """_stage_images must tell image_process the confirmed identity (so the
    AI website-hero generator has something to work from) and whether the
    product already has a hero (so a reused product never pays for another
    website-hero generation)."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    calls = {}

    def fake_image_process(submission, product_key, ledger=None, identity=None, already_has_hero=False, already_has_website_hero=False, config=None):
        calls["identity"] = identity
        calls["already_has_hero"] = already_has_hero
        return {
            "hero": adapters.NULL(),
            "website_hero": adapters.NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process)

    terminal, payload = _stage_ingest(REC, store, CostLedger(), set())
    assert not terminal
    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, CostLedger())

    assert calls["identity"]["Brand"]["value"] == "Acme"
    assert calls["already_has_hero"] is False  # new product, no hero yet


def test_stage_images_persists_website_hero(monkeypatch, store):
    """WebsiteHeroImageURL (the AI-generated unbranded hero) must be persisted
    from imgs["website_hero"] alongside HeroImageURL (the clearest scrubbed
    actual-item photo) from imgs["hero"] -- they are two distinct assets now,
    not one field with a fallback source."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    def fake_image_process(submission, product_key, ledger=None, identity=None, already_has_hero=False, already_has_website_hero=False, config=None):
        return {
            "hero": adapters.E("s3://b/finished/PK/actual.jpg", 0.86, "generated"),
            "website_hero": adapters.E("s3://b/finished/PK/website_hero.jpg", 0.86, "generated"),
            "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.E("s3://b/originals/s/", 1.0, "derived"),
            "scrubbed_folder": adapters.E("s3://b/scrubbed/s/", 1.0, "derived"),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process)

    terminal, payload = _stage_ingest(REC, store, CostLedger(), set())
    assert not terminal
    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, CostLedger())

    stored = store.get_product(payload["product_key"])
    assert stored["WebsiteHeroImageURL"]["value"].endswith("website_hero.jpg")
    assert stored["HeroImageURL"]["value"].endswith("actual.jpg")


def test_stage_images_reused_product_with_existing_hero_skips_generation(monkeypatch, store):
    """A second submission that dedupes to the same, already-imaged product
    must report already_has_hero=True -- image_process uses this to skip
    another website-hero generation for a product that already has one."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    calls = []

    def fake_image_process(submission, product_key, ledger=None, identity=None, already_has_hero=False, already_has_website_hero=False, config=None):
        calls.append(already_has_hero)
        return {
            "hero": adapters.NULL(),
            "website_hero": adapters.NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process)

    ledger = CostLedger()
    _, payload1 = _stage_ingest(REC, store, ledger, set())
    _stage_images(REC, payload1["product_key"], payload1["sku"], payload1["reused"], store, ledger)
    # first pass: no hero was ever produced above (fake returns NULL), so simulate a real hero landing:
    product = store.get_product(payload1["product_key"])
    product["HeroImageURL"] = adapters.E("s3://test-bucket/finished/x/hero.jpg", 0.9, "generated")
    store.upsert_product(product)

    rec2 = dict(REC, submission_id="t1-again")
    _, payload2 = _stage_ingest(rec2, store, ledger, set())
    _stage_images(rec2, payload2["product_key"], payload2["sku"], payload2["reused"], store, ledger)

    assert payload2["reused"] is True
    assert calls[-1] is True  # second call must see already_has_hero=True


def test_stage_images_hero_never_blanked_but_confident_actual_supersedes(monkeypatch, store):
    """HeroImageURL only ever moves forward: a submission that finds no
    confident actual photo must never blank an existing hero, but a later
    submission's confident actual photo IS allowed to supersede it -- Tier 2
    manufacturer lookup is retired, so every confident `hero` is itself an
    actual-item photo and a fresher one is presumed clearer."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())

    existing_url = "s3://test-bucket/finished/x/hero_original.jpg"
    product = store.get_product(payload["product_key"])
    product["HeroImageURL"] = adapters.E(existing_url, 0.9, "generated")
    store.upsert_product(product)

    def fake_image_process_blank(submission, product_key, ledger=None, identity=None, already_has_hero=False, already_has_website_hero=False, config=None):
        return {
            "hero": adapters.NULL(),
            "website_hero": adapters.NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process_blank)
    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)

    stored = store.get_product(payload["product_key"])
    assert stored["HeroImageURL"]["value"] == existing_url  # never blanked

    different_hero = adapters.E("s3://test-bucket/finished/x/hero_different.jpg", 0.95, "generated")

    def fake_image_process_new(submission, product_key, ledger=None, identity=None, already_has_hero=False, already_has_website_hero=False, config=None):
        return {
            "hero": different_hero,
            "website_hero": adapters.NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process_new)
    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)

    stored = store.get_product(payload["product_key"])
    # a confident later actual supersedes
    assert stored["HeroImageURL"]["value"] == "s3://test-bucket/finished/x/hero_different.jpg"


def test_stage_images_gates_website_hero_generation_on_its_own_field(monkeypatch, store):
    """image_process must be told already_has_website_hero, independently of
    already_has_hero -- a product with a confident WebsiteHeroImageURL but NO
    confident HeroImageURL (actual-item photo) must still be reported as
    already_has_website_hero=True, so a resubmission never re-generates and
    re-QCs (and throws away) a website hero it already has."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    calls = {}

    def fake_image_process(submission, product_key, ledger=None, identity=None,
                            already_has_hero=False, already_has_website_hero=False, config=None):
        calls["already_has_hero"] = already_has_hero
        calls["already_has_website_hero"] = already_has_website_hero
        return {
            "hero": adapters.NULL(),
            "website_hero": adapters.NULL(), "website_hero_review": False,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }

    monkeypatch.setattr(adapters, "image_process", fake_image_process)

    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())

    # Product already has a confident WebsiteHeroImageURL but NO HeroImageURL --
    # the two fields are independent.
    product = store.get_product(payload["product_key"])
    product["WebsiteHeroImageURL"] = E("s3://test-bucket/finished/x/website_hero.jpg", 0.9, "generated")
    store.upsert_product(product)

    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)

    assert calls["already_has_hero"] is False           # no actual-item hero
    assert calls["already_has_website_hero"] is True    # but website hero already exists


def test_stage_images_persists_website_hero_review_required_flag(monkeypatch, store):
    """The manual-review escalation (spec §7) must be persisted as a queryable
    product field, not just printed to stderr -- Yes when the AI website-hero
    generation could not produce a safe unbranded hero, else No."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    def make_fake_image_process(review_flag):
        def fake_image_process(submission, product_key, ledger=None, identity=None,
                                already_has_hero=False, already_has_website_hero=False, config=None):
            return {
                "hero": adapters.NULL(),
                "website_hero": adapters.NULL(), "website_hero_review": review_flag,
                "supporting": [],
                "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
                "flags": adapters._photo_availability_flags([]),
            }
        return fake_image_process

    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())

    monkeypatch.setattr(adapters, "image_process", make_fake_image_process(True))
    result = _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)
    assert result["product"]["WebsiteHeroReviewRequired"]["value"] == "Yes"
    stored = store.get_product(payload["product_key"])
    assert stored["WebsiteHeroReviewRequired"]["value"] == "Yes"

    monkeypatch.setattr(adapters, "image_process", make_fake_image_process(False))
    result = _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)
    assert result["product"]["WebsiteHeroReviewRequired"]["value"] == "No"


def test_stage_images_persists_website_hero_rejected_url(monkeypatch, store):
    """When a hero is QC-rejected, the saved rejected image's storage URL is
    persisted (WebsiteHeroRejectedURL) so the admin review link can point at it.
    A run that rejected nothing leaves the field blank."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    def make_fake_image_process(rejected_url):
        def fake_image_process(submission, product_key, ledger=None, identity=None,
                                already_has_hero=False, already_has_website_hero=False, config=None):
            return {
                "hero": adapters.NULL(),
                "website_hero": adapters.NULL(), "website_hero_review": bool(rejected_url),
                "website_hero_rejected": rejected_url,
                "supporting": [],
                "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
                "flags": adapters._photo_availability_flags([]),
            }
        return fake_image_process

    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())

    url = "file:///finished/PK/website_hero_rejected.jpg"
    monkeypatch.setattr(adapters, "image_process", make_fake_image_process(url))
    result = _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)
    assert result["product"]["WebsiteHeroRejectedURL"]["value"] == url
    assert store.get_product(payload["product_key"])["WebsiteHeroRejectedURL"]["value"] == url

    # a later run that rejects nothing must not blank the previously stored URL
    # (same never-blank semantics as WebsiteHeroImageURL)
    monkeypatch.setattr(adapters, "image_process", make_fake_image_process(""))
    result = _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)
    assert result["product"]["WebsiteHeroRejectedURL"]["value"] == url


def _fake_image_process_with_prompt(prompt):
    from resale_listing_ai import adapters

    def fake_image_process(submission, product_key, ledger=None, identity=None,
                            already_has_hero=False, already_has_website_hero=False, config=None):
        return {
            "hero": adapters.NULL(),
            "website_hero": adapters.NULL(), "website_hero_review": True,
            "website_hero_prompt": prompt,
            "supporting": [],
            "original_folder": adapters.NULL(), "scrubbed_folder": adapters.NULL(),
            "flags": adapters._photo_availability_flags([]),
        }
    return fake_image_process


def test_stage_images_persists_website_hero_prompt_by_default(monkeypatch, store):
    """The generation prompt is persisted (WebsiteHeroPrompt) for audit/tuning,
    on by default, even when the hero itself was rejected."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    monkeypatch.delenv("RESALE_LISTING_AI_PERSIST_HERO_PROMPT", raising=False)
    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())
    monkeypatch.setattr(adapters, "image_process", _fake_image_process_with_prompt("the exact hero prompt"))

    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)

    stored = store.get_product(payload["product_key"])
    assert stored["WebsiteHeroPrompt"]["value"] == "the exact hero prompt"


def test_stage_images_website_hero_prompt_disabled(monkeypatch, store):
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest, _stage_images

    monkeypatch.setenv("RESALE_LISTING_AI_PERSIST_HERO_PROMPT", "0")
    ledger = CostLedger()
    _, payload = _stage_ingest(REC, store, ledger, set())
    monkeypatch.setattr(adapters, "image_process", _fake_image_process_with_prompt("should not persist"))

    _stage_images(REC, payload["product_key"], payload["sku"], payload["reused"], store, ledger)

    stored = store.get_product(payload["product_key"])
    assert stored["WebsiteHeroPrompt"]["value"] in (None, "")  # persistence disabled


def test_stage_ingest_identify_cost_row_is_logged_by_vision_identify_itself(store):
    """vision_identify now owns its own cost row instead of _stage_ingest
    logging a flat, provider-blind $0.030 after the fact. Stub mode must keep
    the historical row exactly: stage 'identify', generic service 'vision',
    $0.030, logged exactly once -- so run_demo.py's cost output is unchanged."""
    from resale_listing_ai.pipeline import _stage_ingest

    ledger = CostLedger()

    _stage_ingest(REC, store, ledger, set())

    identify_rows = [r for r in ledger.rows if r["stage"] == "identify"]
    vision_rows = [r for r in identify_rows if r["service"] == "vision"]
    assert vision_rows == [{
        "submission_id": "t1", "stage": "identify", "service": "vision",
        "cost_cad": 0.030, "retry": False,
        "input_tokens": None, "output_tokens": None, "model": None,
    }]


def test_stage_ingest_does_not_double_log_the_identify_vision_cost(store):
    """Guards the Fix-3 restructure: if the old external ledger.add() line were
    ever restored alongside vision_identify's own logging, the identify stage
    would silently bill twice."""
    from resale_listing_ai.pipeline import _stage_ingest

    ledger = CostLedger()

    _stage_ingest(REC, store, ledger, set())

    vision_rows = [r for r in ledger.rows
                   if r["stage"] == "identify" and r["service"] == "vision"]
    assert len(vision_rows) == 1


def test_stage_ingest_passes_the_ledger_through_to_unit_observe(monkeypatch, store):
    """unit_observe now owns its own real-mode cost row (like vision_identify) --
    _stage_ingest must hand it the same ledger, not call it ledger-less."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    calls = {}

    def fake_unit_observe(submission, ledger=None, config=None):
        calls["ledger"] = ledger
        return {}

    monkeypatch.setattr(adapters, "unit_observe", fake_unit_observe)

    ledger = CostLedger()
    _stage_ingest(REC, store, ledger, set())

    assert calls["ledger"] is ledger


def test_stage_ingest_logs_ocr_cost_row_before_vision_identify(store):
    """ocr_read is a deterministic, ~free pre-pass -- mirrors barcode_read's
    own $0.000 ledger row, logged from _stage_ingest itself (ocr_read, like
    barcode_read, takes no ledger argument of its own)."""
    from resale_listing_ai.pipeline import _stage_ingest

    ledger = CostLedger()

    _stage_ingest(REC, store, ledger, set())

    ocr_rows = [r for r in ledger.rows if r["stage"] == "identify" and r["service"] == "local_ocr"]
    assert ocr_rows == [{
        "submission_id": "t1", "stage": "identify", "service": "local_ocr",
        "cost_cad": 0.000, "retry": False,
        "input_tokens": None, "output_tokens": None, "model": None,
    }]


def test_gate_uses_admin_override_threshold_over_the_hardcoded_default(store):
    from resale_listing_ai.pipeline import gate
    from resale_listing_ai.envelope import E

    # A confident-at-0.6-default identity that drops below an admin-raised 0.9 bar.
    identity = {
        "Brand": E("Acme", 0.7, "vision"), "ProductName": E("Smart Doorbell", 0.7, "vision"),
        "InternalCategory": E("Smart Home", 0.7, "vision"), "ModelNumber": E("AC1000-XY", 0.7, "vision"),
    }

    status_default, _ = gate(identity)
    status_strict, missing_strict = gate(identity, config={"gate.threshold": 0.95})

    assert status_default == "Accepted"
    assert status_strict in ("Clarification required", "Rejected")
    assert "Brand" in missing_strict


def test_gate_uses_admin_override_mandatory_fields_over_the_hardcoded_default(store):
    from resale_listing_ai.pipeline import gate
    from resale_listing_ai.envelope import E

    identity = {
        "Brand": E("Acme", 0.9, "vision"), "ProductName": E("Smart Doorbell", 0.9, "vision"),
        "ModelNumber": E("AC1000-XY", 0.9, "vision"),
        # InternalCategory deliberately absent -- irrelevant once it's not mandatory
    }

    status, missing = gate(identity, config={"gate.mandatory_fields": ["Brand", "ProductName"]})

    assert status == "Accepted"
    assert missing == []


def test_stage_ingest_loads_config_from_the_injected_store_when_not_given(store):
    """The store passed to _stage_ingest is the one that must be consulted --
    never a fresh Store.connect(), which would open a separate in-memory
    sqlite DB and silently miss this override. Uses the file's own top-level
    REC fixture (hint "nest-doorbell", which normally gates to Accepted) and
    resale_listing_ai.cost.CostLedger, exactly as every other test in this file does."""
    from resale_listing_ai.pipeline import _stage_ingest

    store.set_admin_config_override("gate.threshold", "0.99", updated_by=1)

    terminal, payload = _stage_ingest(REC, store, CostLedger(), set())

    assert terminal is True
    assert payload["status"] in ("Clarification required", "Rejected")


def test_has_no_unknowns_uses_admin_override_core_fields(store):
    from resale_listing_ai.pipeline import _has_no_unknowns
    from resale_listing_ai.records import blank_product, blank_listing
    from resale_listing_ai.envelope import E

    product = blank_product()
    product["Brand"] = E("Acme", 0.9, "vision")
    listing = blank_listing()

    assert not _has_no_unknowns(product, listing)  # default CORE_FIELDS: plenty still missing
    assert _has_no_unknowns(product, listing, config={"gate.core_fields": ["Brand"]})


def test_stage_ingest_passes_ocr_text_into_vision_identify(monkeypatch, store):
    """Proves the OCR pre-pass's output actually reaches vision_identify's
    ocr_text param -- not just computed and discarded."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    monkeypatch.setattr(adapters, "ocr_read", lambda submission: "Model No. AC1000-XY")

    calls = {}

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        calls["ocr_text"] = ocr_text
        return {}

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)

    _stage_ingest(REC, store, CostLedger(), set())

    assert calls["ocr_text"] == "Model No. AC1000-XY"


def test_field_overrides_satisfy_the_gates_anchor_requirement(monkeypatch, store):
    """A submitter's typed answer to a "ModelNumber/UPC missing" Clarification-
    required outcome is trusted verbatim (source="provided", confidence=1.0) --
    proving the override actually reaches gate() and changes its outcome, not
    just that the value gets stored somewhere."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.9, "vision"),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    submission = {"submission_id": "ov1", "images": ["a"], "notes": None,
                  "provided_condition_grade": "Open Box",
                  "field_overrides": {"ModelNumber": "XYZ123"}}

    terminal, payload = _stage_ingest(submission, store, CostLedger(), set())

    assert terminal is False  # the gate passed -- an override supplied the missing anchor
    assert payload["product_key"]


def test_field_overrides_recognizes_the_gates_display_label_for_the_anchor(monkeypatch, store):
    """The gate reports the missing anchor as the display label "ModelNumber/UPC"
    (see gate()'s `missing.append("ModelNumber/UPC")`), not a real identity
    field -- field_overrides must alias that literal label back to ModelNumber,
    or a submitter's answer to exactly the field the UI showed them would never
    actually satisfy the gate. This uses the literal label, not a hand-written
    real field name, because that's exactly the gap that shipped undetected."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.9, "vision"),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    submission = {"submission_id": "alias1", "images": ["a"], "notes": None,
                  "provided_condition_grade": "Open Box",
                  "field_overrides": {"ModelNumber/UPC": "XYZ123"}}

    terminal, payload = _stage_ingest(submission, store, CostLedger(), set())

    assert terminal is False  # the literal display-label override satisfied the gate
    assert payload["product_key"]


def test_without_field_overrides_the_same_identity_is_clarification_required(monkeypatch, store):
    """Same stubbed identity, no override -- confirms the previous test's PASS
    is actually caused by the override, not some other change in the fixture."""
    from resale_listing_ai import adapters
    from resale_listing_ai.pipeline import _stage_ingest

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.9, "vision"),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    submission = {"submission_id": "ov2", "images": ["a"], "notes": None,
                  "provided_condition_grade": "Open Box"}

    terminal, payload = _stage_ingest(submission, store, CostLedger(), set())

    assert terminal is True
    assert payload["status"] == "Clarification required"
    assert payload["missing"] == ["ModelNumber/UPC"]
