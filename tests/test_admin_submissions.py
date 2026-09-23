"""Tests for GET /v1/admin/submissions (resale_listing_ai/api.py)."""

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai import auth as auth_module
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_API_KEYS", "test-key")
    monkeypatch.setenv("RESALE_LISTING_AI_COOKIE_SECURE", "0")


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture
def queues():
    return {"ingest": LocalQueue("ingest-queue"), "image": LocalQueue("image-queue")}


@pytest.fixture
def client(store, queues):
    from resale_listing_ai.api import create_app

    return TestClient(create_app(store=store, queues=queues))


def _admin_cookies(store):
    user_id = store.create_user("admin@example.com", auth_module.hash_password("hunter22"), role="admin")
    session_id = auth_module.new_session_id()
    store.create_session(session_id, user_id, auth_module.session_expiry())
    return {"session_id": session_id}


def test_get_admin_submissions_returns_401_for_anonymous(client):
    resp = client.get("/v1/admin/submissions")
    assert resp.status_code == 401


def test_get_admin_submissions_returns_submissions_and_total(client, store):
    store.record_submission("sub-1", "Accepted", {"product_key": "PK-1", "listing_sku": "SKU-1", "cost_cad": 2.5})
    cookies = _admin_cookies(store)

    resp = client.get("/v1/admin/submissions", cookies=cookies)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["submissions"][0]["submission_id"] == "sub-1"
    assert body["submissions"][0]["cost_cad"] == 2.5


def test_get_admin_submissions_filters_by_status(client, store):
    store.record_submission("sub-accepted", "Accepted", {"product_key": "PK-1", "listing_sku": "SKU-1"})
    store.record_submission("sub-rejected", "Rejected", {"product_key": None, "listing_sku": None})
    cookies = _admin_cookies(store)

    resp = client.get("/v1/admin/submissions?status=Rejected", cookies=cookies)

    body = resp.json()
    assert body["total"] == 1
    assert body["submissions"][0]["submission_id"] == "sub-rejected"


def test_get_admin_submissions_rejects_limit_over_200(client, store):
    cookies = _admin_cookies(store)

    resp = client.get("/v1/admin/submissions?limit=500", cookies=cookies)

    assert resp.status_code == 422


def test_get_admin_submissions_rejects_negative_offset(client, store):
    # A negative offset passes straight to Store.list_submissions, which is
    # fine on sqlite but raises an unhandled 500 ("OFFSET must not be
    # negative") on PostgreSQL, the actual deploy dialect.
    cookies = _admin_cookies(store)

    resp = client.get("/v1/admin/submissions?offset=-1", cookies=cookies)

    assert resp.status_code == 422


# ---- AI website-hero review link -------------------------------------------

def _record_with_rejected_hero(store, submission_id, product_key, rejected_url):
    """Persist a DONE submission whose product carries a QC-rejected website hero,
    the shape _stage_images produces on a review-flagged run."""
    from resale_listing_ai import adapters, records

    product = records.blank_product()
    product["ProductKey"] = adapters.E(product_key, 1.0, "derived")
    product["WebsiteHeroReviewRequired"] = adapters.E("Yes", 1.0, "derived")
    product["WebsiteHeroRejectedURL"] = adapters.E(rejected_url, 1.0, "derived")
    product["WebsiteHeroPrompt"] = adapters.E("the exact hero prompt", 1.0, "derived")
    store.upsert_product(product)
    store.record_submission(submission_id, "Accepted",
                            {"product_key": product_key, "listing_sku": "SKU-1",
                             "cost_cad": 1.0, "product": product})


def test_admin_submissions_row_includes_website_hero_review_fields(client, store):
    _record_with_rejected_hero(store, "sub-hero", "PK-HERO", "file:///tmp/x/website_hero_rejected.jpg")
    cookies = _admin_cookies(store)

    row = client.get("/v1/admin/submissions", cookies=cookies).json()["submissions"][0]

    assert row["website_hero_review"] == "Yes"
    assert row["website_hero_rejected_url"] == "file:///tmp/x/website_hero_rejected.jpg"
    assert row["website_hero_prompt"] == "the exact hero prompt"


def test_admin_submissions_row_hero_fields_null_when_absent(client, store):
    store.record_submission("sub-plain", "Accepted",
                            {"product_key": "PK-1", "listing_sku": "SKU-1", "cost_cad": 2.5})
    cookies = _admin_cookies(store)

    row = client.get("/v1/admin/submissions", cookies=cookies).json()["submissions"][0]

    assert row["website_hero_review"] is None
    assert row["website_hero_rejected_url"] is None


def test_get_admin_hero_image_requires_admin(client):
    assert client.get("/v1/admin/hero-image/sub-hero").status_code == 401


def test_get_admin_hero_image_streams_rejected_bytes(client, store, tmp_path):
    img = tmp_path / "website_hero_rejected.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")  # JPEG magic + payload
    _record_with_rejected_hero(store, "sub-hero", "PK-HERO", f"file://{img}")
    cookies = _admin_cookies(store)

    resp = client.get("/v1/admin/hero-image/sub-hero", cookies=cookies)

    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff\xe0jpeg-bytes"
    assert resp.headers["content-type"] == "image/jpeg"


def test_get_admin_hero_image_404_when_no_rejected_url(client, store):
    store.record_submission("sub-plain", "Accepted",
                            {"product_key": "PK-1", "listing_sku": "SKU-1"})
    # product exists but has no rejected hero
    from resale_listing_ai import adapters, records
    p = records.blank_product()
    p["ProductKey"] = adapters.E("PK-1", 1.0, "derived")
    store.upsert_product(p)
    cookies = _admin_cookies(store)

    assert client.get("/v1/admin/hero-image/sub-plain", cookies=cookies).status_code == 404


def test_get_admin_hero_image_404_when_missing_file(client, store):
    _record_with_rejected_hero(store, "sub-hero", "PK-HERO", "file:///tmp/does-not-exist.jpg")
    cookies = _admin_cookies(store)

    assert client.get("/v1/admin/hero-image/sub-hero", cookies=cookies).status_code == 404


def test_get_admin_hero_image_404_when_submission_unknown(client, store):
    cookies = _admin_cookies(store)
    assert client.get("/v1/admin/hero-image/nope", cookies=cookies).status_code == 404
