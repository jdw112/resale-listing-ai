"""Live-Postgres verification of resale_listing_ai/db.py — the real production path.

Skipped by default (no config needed to keep the main suite green offline). Opt in
with RESALE_LISTING_AI_DB_MODE=postgres and DATABASE_URL set (directly, or via .env — see
.env.example), e.g.:

    RESALE_LISTING_AI_DB_MODE=postgres .venv/bin/python -m pytest -q tests/test_db_live.py

Every test cleans up the rows it wrote (DELETE, never DROP) since this may run
against a real shared server — safe to run repeatedly.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RESALE_LISTING_AI_DB_MODE") != "postgres",
    reason="live Postgres tests need RESALE_LISTING_AI_DB_MODE=postgres (+ DATABASE_URL); skipped by default",
)


@pytest.fixture
def store():
    from resale_listing_ai.db import Store

    try:
        s = Store.connect()
        s.ensure_schema()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"live Postgres not reachable: {exc}")
    yield s
    s.close()


def test_store_connect_uses_postgres_dialect_when_configured(store):
    assert store.dialect == "postgres"


def test_live_upsert_product_on_conflict_do_update_reuses_the_row(store):
    from resale_listing_ai.envelope import E, NULL
    from resale_listing_ai.records import blank_product

    key = "TEST-LIVE-PRODUCT-KEY"
    try:
        product = blank_product()
        product["ProductKey"] = E(key, 1.0, "derived")
        product["HeroImageURL"] = NULL()
        store.upsert_product(product)

        updated = dict(product)
        updated["HeroImageURL"] = E("s3://bucket/hero.png", 0.86, "generated")
        store.upsert_product(updated)

        fetched = store.get_product(key)
        assert fetched["HeroImageURL"]["value"] == "s3://bucket/hero.png"

        cur = store._execute('SELECT COUNT(*) FROM product WHERE "ProductKey" = %s', (key,))
        assert cur.fetchone()[0] == 1
    finally:
        store._execute('DELETE FROM product WHERE "ProductKey" = %s', (key,))
        store._commit()


def test_live_submission_idempotency_round_trip(store):
    sid = "TEST-LIVE-SUBMISSION-1"
    try:
        result = {"submission_id": sid, "status": "Accepted with unknowns", "cost_cad": 0.14}

        assert store.get_submission_result(sid) is None
        store.record_submission(sid, "Accepted with unknowns", result)

        assert store.get_submission_result(sid) == result
    finally:
        store._execute("DELETE FROM submission WHERE submission_id = %s", (sid,))
        store._commit()


def test_live_cost_rows_are_plain_floats_not_decimal(store):
    """NUMERIC columns come back as Decimal from psycopg — db.py uses DOUBLE
    PRECISION instead so cost figures stay plain, JSON-serializable floats."""
    import json

    sid = "TEST-LIVE-COST-ROWS"
    try:
        store.record_cost_rows([
            {"submission_id": sid, "stage": "identify", "service": "vision", "cost_cad": 0.03, "retry": False},
        ])
        rows = store.cost_rows(sid)

        assert isinstance(rows[0]["cost_cad"], float)
        json.dumps(rows[0])  # must not raise on a Decimal
    finally:
        store._execute("DELETE FROM cost_ledger WHERE submission_id = %s", (sid,))
        store._commit()


def test_live_pipeline_dedupe_and_reprocessing_no_op():
    """Full run() against the real server: a second unit reuses the Product row,
    and redelivering the same submission_id is a safe no-op."""
    from resale_listing_ai.cost import CostLedger
    from resale_listing_ai.db import Store
    from resale_listing_ai.pipeline import run

    try:
        store = Store.connect()
        store.ensure_schema()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"live Postgres not reachable: {exc}")

    os.environ["RESALE_LISTING_AI_STUBS"] = "1"
    rec = {"submission_id": "TEST-LIVE-E2E-1", "hint": "nest-doorbell", "images": ["a"],
           "notes": None, "provided_condition_grade": "Open Box"}
    rec2 = {**rec, "submission_id": "TEST-LIVE-E2E-2"}
    skus, ledger = set(), CostLedger()

    try:
        a = run(rec, store, ledger, skus)
        b = run(rec2, store, ledger, skus)
        assert a["product_key"] == b["product_key"]

        rows_before = len(store.cost_rows(rec["submission_id"]))
        replay = run(rec, store, ledger, skus)
        assert replay == a
        assert len(store.cost_rows(rec["submission_id"])) == rows_before
    finally:
        for sid in (rec["submission_id"], rec2["submission_id"]):
            store._execute("DELETE FROM submission WHERE submission_id = %s", (sid,))
            store._execute("DELETE FROM cost_ledger WHERE submission_id = %s", (sid,))
        store._execute('DELETE FROM listing WHERE "SKU" = %s', (a["listing_sku"],))
        store._execute('DELETE FROM listing WHERE "SKU" = %s', (b["listing_sku"],))
        store._execute('DELETE FROM product WHERE "ProductKey" = %s', (a["product_key"],))
        store._commit()
        store.close()
