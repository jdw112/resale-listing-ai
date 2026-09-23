"""Tests for resale_listing_ai/db.py — Postgres-as-source-of-truth store.

These all run against the offline sqlite fallback (RESALE_LISTING_AI_DB_MODE unset/`sqlite`,
the default) so they're green with zero setup, same philosophy as RESALE_LISTING_AI_STUBS for
the adapters. The upsert/idempotency SQL is dialect-independent (verified in
build_schema_sql tests below), so exercising it for real against sqlite's real
ON CONFLICT DO UPDATE engine is meaningful evidence, not a Python-side simulation.
Live-Postgres tests (against a real DATABASE_URL) live in tests/test_db_live.py and
skip themselves when no live server is configured.
"""

import json

import pytest

from resale_listing_ai.envelope import E, NULL
from resale_listing_ai.records import LISTING_FIELDS, PRODUCT_FIELDS, blank_listing, blank_product

# ---------------------------------------------------------------------------
# build_schema_sql — pure DDL generation, no DB needed
# ---------------------------------------------------------------------------

def test_build_schema_sql_product_table_has_every_field_pk_and_field_meta():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    product_sql = next(s for s in statements if "CREATE TABLE IF NOT EXISTS product " in s or "product (" in s)

    for field in PRODUCT_FIELDS:
        assert f'"{field}"' in product_sql
    assert '"field_meta"' in product_sql or "field_meta" in product_sql
    assert 'PRIMARY KEY ("ProductKey")' in product_sql


def test_build_schema_sql_listing_table_primary_key_is_sku():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    listing_sql = next(s for s in statements if s.strip().startswith("CREATE TABLE IF NOT EXISTS listing"))

    for field in LISTING_FIELDS:
        assert f'"{field}"' in listing_sql
    assert 'PRIMARY KEY ("SKU")' in listing_sql


def test_build_schema_sql_submission_table_keyed_by_submission_id():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    submission_sql = next(s for s in statements if s.strip().startswith("CREATE TABLE IF NOT EXISTS submission"))

    assert '"submission_id"' in submission_sql
    assert "PRIMARY KEY" in submission_sql
    assert '"result_json"' in submission_sql


def test_build_schema_sql_cost_ledger_is_append_only_with_autoincrement_id():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    ledger_sql = next(s for s in statements if s.strip().startswith("CREATE TABLE IF NOT EXISTS cost_ledger"))

    assert "AUTOINCREMENT" in ledger_sql
    assert '"submission_id"' in ledger_sql
    assert '"cost_cad"' in ledger_sql


def test_build_schema_sql_postgres_dialect_uses_serial_not_autoincrement():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("postgres")
    ledger_sql = next(s for s in statements if s.strip().startswith("CREATE TABLE IF NOT EXISTS cost_ledger"))

    assert "SERIAL" in ledger_sql
    assert "AUTOINCREMENT" not in ledger_sql


# ---------------------------------------------------------------------------
# _flatten_record / _inflate_record — pure envelope <-> (values, field_meta)
# ---------------------------------------------------------------------------

def test_flatten_record_splits_values_from_confidence_and_source():
    from resale_listing_ai.db import _flatten_record

    product = blank_product()
    product["Brand"] = E("Acme", 0.9, "vision")

    values, meta = _flatten_record(product, PRODUCT_FIELDS)

    assert values["Brand"] == "Acme"
    assert values["ProductName"] is None
    assert meta["Brand"] == {"confidence": 0.9, "source": "vision"}
    assert meta["ProductName"] == {"confidence": 0.0, "source": "none"}


def test_inflate_record_is_the_exact_inverse_of_flatten():
    from resale_listing_ai.db import _flatten_record, _inflate_record

    product = blank_product()
    product["Brand"] = E("Acme", 0.9, "vision")
    product["ModelNumber"] = E("AC1000-XY", 0.94, "label_ocr")

    values, meta = _flatten_record(product, PRODUCT_FIELDS)
    restored = _inflate_record(values, meta, PRODUCT_FIELDS)

    assert restored == product


def test_inflate_record_never_fabricates_a_field_missing_from_meta():
    from resale_listing_ai.db import _inflate_record

    restored = _inflate_record({"Brand": "Acme"}, {}, ["Brand", "ProductName"])

    assert restored["Brand"] == {"value": "Acme", "confidence": 0.0, "source": "none"}
    assert restored["ProductName"] == {"value": None, "confidence": 0.0, "source": "none"}


# ---------------------------------------------------------------------------
# _load_dotenv — stdlib-only, sets only UNSET env vars from a .env-style file
# ---------------------------------------------------------------------------

def test_load_dotenv_sets_unset_env_vars_from_file(monkeypatch, tmp_path):
    from resale_listing_ai.db import _load_dotenv

    monkeypatch.delenv("RESALE_LISTING_AI_TEST_DOTENV_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("RESALE_LISTING_AI_TEST_DOTENV_VAR=hello\n# a comment\n\nOTHER=1\n")

    _load_dotenv(env_file)

    assert __import__("os").environ["RESALE_LISTING_AI_TEST_DOTENV_VAR"] == "hello"


def test_load_dotenv_never_overrides_an_already_set_env_var(monkeypatch, tmp_path):
    from resale_listing_ai.db import _load_dotenv

    monkeypatch.setenv("RESALE_LISTING_AI_TEST_DOTENV_VAR", "real-value")
    env_file = tmp_path / ".env"
    env_file.write_text("RESALE_LISTING_AI_TEST_DOTENV_VAR=from-file\n")

    _load_dotenv(env_file)

    assert __import__("os").environ["RESALE_LISTING_AI_TEST_DOTENV_VAR"] == "real-value"


# ---------------------------------------------------------------------------
# Store — sqlite dialect (default, offline, always green)
# ---------------------------------------------------------------------------

@pytest.fixture
def store(monkeypatch):
    from resale_listing_ai.db import Store

    # setenv, not delenv: _load_dotenv() uses os.environ.setdefault(), so a
    # deleted var just gets silently re-populated from a real repo .env on
    # the next Store.connect() call -- if a developer's local .env has
    # RESALE_LISTING_AI_DB_MODE=postgres uncommented (a documented, supported way to
    # point this repo at live Postgres), delenv-only isolation here would
    # silently run these tests against that real server instead of sqlite.
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


def test_store_connect_defaults_to_sqlite_with_no_config(monkeypatch, tmp_path):
    """True "no config" case: neutralize the real repo .env (rather than
    relying on delenv, which _load_dotenv()'s setdefault() would undo) so
    this only tests Store.connect()'s actual built-in default."""
    from resale_listing_ai import db

    monkeypatch.delenv("RESALE_LISTING_AI_DB_MODE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)

    store = db.Store.connect()
    assert store.dialect == "sqlite"


def test_store_upsert_and_get_product_round_trips_full_envelope(store):
    product = blank_product()
    product["ProductKey"] = E("ACME-AC1000XY-ASH", 1.0, "derived")
    product["Brand"] = E("Acme", 0.9, "vision")
    product["ProductName"] = E("Smart Doorbell", 0.9, "vision")

    store.upsert_product(product)
    fetched = store.get_product("ACME-AC1000XY-ASH")

    assert fetched == product


def test_store_get_product_returns_none_when_absent(store):
    assert store.get_product("NO-SUCH-KEY") is None


def test_store_upsert_product_on_conflict_overwrites_existing_row(store):
    """The real dedupe mechanism: a second unit of the same product upserts onto
    the SAME row rather than creating a new one — proven here against a real SQL
    ON CONFLICT DO UPDATE, not a Python re-implementation of the semantics."""
    product = blank_product()
    product["ProductKey"] = E("ACME-AC1000XY-ASH", 1.0, "derived")
    product["HeroImageURL"] = NULL()
    store.upsert_product(product)

    updated = dict(product)
    updated["HeroImageURL"] = E("s3://bucket/hero.png", 0.86, "generated")
    store.upsert_product(updated)

    fetched = store.get_product("ACME-AC1000XY-ASH")
    assert fetched["HeroImageURL"]["value"] == "s3://bucket/hero.png"

    cur = store._execute("SELECT COUNT(*) FROM product")
    assert cur.fetchone()[0] == 1  # still exactly one row -> reused, not duplicated


def test_store_upsert_and_get_listing_round_trips(store):
    listing = blank_listing()
    listing["SKU"] = E("RL-AAAA-111", 1.0, "generated")
    listing["ProvidedConditionGrade"] = E("Open Box", 1.0, "provided")

    store.upsert_listing(listing)
    fetched = store.get_listing("RL-AAAA-111")

    assert fetched == listing


def test_store_submission_idempotency_round_trip(store):
    result = {"submission_id": "sub-1", "status": "Accepted with unknowns", "product_key": "X"}

    assert store.get_submission_result("sub-1") is None

    store.record_submission("sub-1", "Accepted with unknowns", result)

    assert store.get_submission_result("sub-1") == result


# ---------------------------------------------------------------------------
# Per-stage idempotency (submission.state) — for the two-phase worker runtime:
# ingest-phase and images-phase are each independently checkable, not just
# "is the whole submission done."
# ---------------------------------------------------------------------------

def test_store_get_submission_state_returns_none_when_never_touched(store):
    assert store.get_submission_state("never-seen") is None


def test_store_mark_submission_ingested_sets_state_and_keys_without_a_result(store):
    store.mark_submission_ingested("sub-2", "PRODKEY", "RL-AAAA-111")

    state = store.get_submission_state("sub-2")

    assert state == {"state": "INGESTED", "product_key": "PRODKEY", "sku": "RL-AAAA-111", "result": None,
                     "user_id": None, "parent_submission_id": None, "original_payload": None}


def test_store_mark_submission_received_records_a_state_before_any_worker_runs(store):
    store.mark_submission_received("sub-received")

    assert store.get_submission_state("sub-received") == {
        "state": "RECEIVED", "product_key": None, "sku": None, "result": None,
        "user_id": None, "parent_submission_id": None, "original_payload": None}


def test_store_mark_submission_received_never_regresses_a_more_advanced_state(store):
    """A race between the API's own record and a worker that already made
    progress must not roll the state backwards."""
    store.mark_submission_ingested("sub-race", "PRODKEY", "RL-AAAA-111")

    store.mark_submission_received("sub-race")

    assert store.get_submission_state("sub-race")["state"] == "INGESTED"


def test_store_get_submission_result_returns_none_for_ingested_but_not_done(store):
    """The ingest phase alone must never look "complete" to the top-level
    idempotency check — only a terminal record_submission() call does."""
    store.mark_submission_ingested("sub-3", "PRODKEY", "RL-AAAA-111")

    assert store.get_submission_result("sub-3") is None


def test_store_record_submission_sets_state_done_and_result(store):
    result = {"submission_id": "sub-4", "status": "Accepted with unknowns",
              "product_key": "PRODKEY", "listing_sku": "RL-AAAA-111"}

    store.record_submission("sub-4", "Accepted with unknowns", result)

    state = store.get_submission_state("sub-4")
    assert state["state"] == "DONE"
    assert state["product_key"] == "PRODKEY"
    assert state["sku"] == "RL-AAAA-111"
    assert state["result"] == result
    assert store.get_submission_result("sub-4") == result


def test_store_record_submission_after_mark_ingested_transitions_to_done(store):
    """The realistic sequence: ingest worker marks INGESTED, image worker later
    finalizes with record_submission -> state moves to DONE, same row."""
    store.mark_submission_ingested("sub-5", "PRODKEY", "RL-AAAA-111")

    result = {"submission_id": "sub-5", "status": "Accepted with unknowns"}
    store.record_submission("sub-5", "Accepted with unknowns", result)

    assert store.get_submission_state("sub-5")["state"] == "DONE"
    assert store.count("submission") == 1  # one row throughout, not two


def test_store_record_cost_rows_appends_and_is_queryable(store):
    rows = [
        {"submission_id": "sub-1", "stage": "identify", "service": "vision", "cost_cad": 0.03, "retry": False},
        {"submission_id": "sub-1", "stage": "pricing", "service": "web_search", "cost_cad": 0.04, "retry": False},
    ]

    store.record_cost_rows(rows)

    fetched = store.cost_rows("sub-1")
    assert len(fetched) == 2
    assert {r["stage"] for r in fetched} == {"identify", "pricing"}


def test_store_cost_rows_round_trip_token_detail(store):
    rows = [
        {"submission_id": "sub-tok", "stage": "identify", "service": "openai_vision",
         "cost_cad": 0.000621, "retry": False,
         "input_tokens": 1000, "output_tokens": 500, "model": "gpt-4o-mini"},
    ]

    store.record_cost_rows(rows)

    fetched = store.cost_rows("sub-tok")
    assert len(fetched) == 1
    assert fetched[0]["input_tokens"] == 1000
    assert fetched[0]["output_tokens"] == 500
    assert fetched[0]["model"] == "gpt-4o-mini"


def test_store_cost_rows_token_detail_optional_stays_null(store):
    """A row logged the old way (no token detail) still stores and reads back
    with null token columns -- backward compatible."""
    store.record_cost_rows([
        {"submission_id": "sub-flat", "stage": "identify", "service": "vision",
         "cost_cad": 0.03, "retry": False},
    ])

    fetched = store.cost_rows("sub-flat")
    assert fetched[0]["input_tokens"] is None
    assert fetched[0]["output_tokens"] is None
    assert fetched[0]["model"] is None


def test_store_export_csv_contains_only_flattened_values_not_field_meta(store, tmp_path):
    product = blank_product()
    product["ProductKey"] = E("ACME-AC1000XY-ASH", 1.0, "derived")
    product["Brand"] = E("Acme", 0.9, "vision")
    store.upsert_product(product)

    out = tmp_path / "product.csv"
    store.export_csv("product", PRODUCT_FIELDS, out)

    import csv

    with open(out) as fh:
        rows = list(csv.DictReader(fh))

    assert rows[0]["Brand"] == "Acme"
    assert "field_meta" not in rows[0]
    assert "confidence" not in rows[0]


def test_store_create_and_get_user_by_email(store):
    user_id = store.create_user("jane@example.com", "hashed-pw", role="submitter")

    fetched = store.get_user_by_email("jane@example.com")

    assert fetched["id"] == user_id
    assert fetched["email"] == "jane@example.com"
    assert fetched["password_hash"] == "hashed-pw"
    assert fetched["role"] == "submitter"


def test_store_get_user_by_email_returns_none_when_absent(store):
    assert store.get_user_by_email("nobody@example.com") is None


def test_store_get_user_by_id_round_trips(store):
    user_id = store.create_user("jane@example.com", "hashed-pw")

    fetched = store.get_user_by_id(user_id)

    assert fetched["email"] == "jane@example.com"


def test_store_get_user_by_id_returns_none_when_absent(store):
    assert store.get_user_by_id(999999) is None


def test_store_create_session_and_get_session_round_trips(store):
    user_id = store.create_user("jane@example.com", "hashed-pw")

    store.create_session("sess-abc123", user_id, "2099-01-01T00:00:00+00:00")
    fetched = store.get_session("sess-abc123")

    assert fetched["user_id"] == user_id
    assert fetched["expires_at"] == "2099-01-01T00:00:00+00:00"


def test_store_get_session_returns_none_when_absent(store):
    assert store.get_session("no-such-session") is None


def test_store_delete_session_removes_it(store):
    user_id = store.create_user("jane@example.com", "hashed-pw")
    store.create_session("sess-abc123", user_id, "2099-01-01T00:00:00+00:00")

    store.delete_session("sess-abc123")

    assert store.get_session("sess-abc123") is None


def test_build_schema_sql_includes_app_user_and_session_tables():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    user_sql = next(s for s in statements if s.strip().startswith('CREATE TABLE IF NOT EXISTS app_user'))
    session_sql = next(s for s in statements if s.strip().startswith('CREATE TABLE IF NOT EXISTS session'))

    assert '"email"' in user_sql
    assert '"password_hash"' in user_sql
    assert '"role"' in user_sql
    assert '"session_id"' in session_sql
    assert 'PRIMARY KEY' in session_sql


def test_admin_config_override_round_trips(store):
    store.set_admin_config_override("gate.threshold", '{"value": 0.7}', updated_by=1)

    row = store.get_admin_config_override("gate.threshold")

    assert row["key"] == "gate.threshold"
    assert row["value_json"] == '{"value": 0.7}'
    assert row["updated_by"] == 1
    assert row["updated_at"] is not None


def test_admin_config_override_unset_key_returns_none(store):
    assert store.get_admin_config_override("gate.threshold") is None


def test_admin_config_override_upsert_replaces_value(store):
    store.set_admin_config_override("gate.threshold", '{"value": 0.7}', updated_by=1)
    store.set_admin_config_override("gate.threshold", '{"value": 0.8}', updated_by=2)

    row = store.get_admin_config_override("gate.threshold")

    assert row["value_json"] == '{"value": 0.8}'
    assert row["updated_by"] == 2


def test_get_admin_config_overrides_lists_everything_set(store):
    store.set_admin_config_override("gate.threshold", "0.7", updated_by=1)
    store.set_admin_config_override("provider_stack.vision_provider", '"gemini"', updated_by=1)

    rows = store.get_admin_config_overrides()

    assert {r["key"] for r in rows} == {"gate.threshold", "provider_stack.vision_provider"}


def test_get_admin_config_overrides_empty_when_nothing_set(store):
    assert store.get_admin_config_overrides() == []


def test_delete_admin_config_override_removes_it(store):
    store.set_admin_config_override("gate.threshold", "0.7", updated_by=1)

    store.delete_admin_config_override("gate.threshold")

    assert store.get_admin_config_override("gate.threshold") is None


def test_delete_admin_config_override_is_a_no_op_for_an_unset_key(store):
    store.delete_admin_config_override("gate.threshold")  # must not raise


# ---------------------------------------------------------------------------
# Admin: submissions listing
# ---------------------------------------------------------------------------

def test_list_submissions_returns_terminal_and_in_flight_rows(store):
    store.mark_submission_received("sub-1")
    store.mark_submission_ingested("sub-2", "PK-1", "SKU-1")
    store.record_submission("sub-3", "Accepted", {
        "product_key": "PK-2", "listing_sku": "SKU-2", "cost_cad": 1.23})

    rows = store.list_submissions()

    ids = {r["submission_id"] for r in rows}
    assert ids == {"sub-1", "sub-2", "sub-3"}
    done_row = next(r for r in rows if r["submission_id"] == "sub-3")
    assert done_row["status"] == "Accepted"
    assert done_row["cost_cad"] == 1.23
    in_flight_row = next(r for r in rows if r["submission_id"] == "sub-1")
    assert in_flight_row["cost_cad"] is None


def test_list_submissions_filters_by_status(store):
    store.record_submission("sub-done", "Accepted", {"product_key": "PK-1", "listing_sku": "SKU-1"})
    store.record_submission("sub-rejected", "Rejected", {"product_key": None, "listing_sku": None})

    rows = store.list_submissions(status="Accepted")

    assert [r["submission_id"] for r in rows] == ["sub-done"]


def test_list_submissions_respects_limit_and_offset(store):
    for i in range(5):
        store.mark_submission_received(f"sub-{i}")

    page1 = store.list_submissions(limit=2, offset=0)
    page2 = store.list_submissions(limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["submission_id"] for r in page1}.isdisjoint({r["submission_id"] for r in page2})


def test_list_submissions_orders_deterministically_with_null_processed_at_last(store, monkeypatch):
    """sqlite sorts NULLs last under ORDER BY ... DESC by default, PostgreSQL
    sorts NULLs FIRST -- list_submissions's ORDER BY must force NULL
    processed_at (in-flight submissions) last on both dialects, and give
    same/tied processed_at values a fully deterministic secondary order.
    No current Store method leaves processed_at NULL (mark_submission_received
    stamps it too), but the column is nullable by schema, so the row is built
    directly here to exercise that state regardless."""
    from resale_listing_ai import db

    # Two DONE rows with distinct, controlled timestamps -- monkeypatched so
    # the assertion never depends on real-clock timing being fast enough to
    # actually differ between two calls.
    monkeypatch.setattr(db, "_now_iso", lambda: "2026-01-01T00:00:00+00:00")
    store.record_submission("sub-older", "Accepted", {"product_key": "PK-1", "listing_sku": "SKU-1"})
    monkeypatch.setattr(db, "_now_iso", lambda: "2026-01-02T00:00:00+00:00")
    store.record_submission("sub-newer", "Accepted", {"product_key": "PK-2", "listing_sku": "SKU-2"})

    # An in-flight row with a genuinely NULL processed_at.
    store._execute(
        f'INSERT INTO submission (submission_id, state, processed_at) VALUES ({store._ph}, {store._ph}, NULL)',
        ("sub-inflight", "RECEIVED"))
    store._commit()

    rows = store.list_submissions()

    assert [r["submission_id"] for r in rows] == ["sub-newer", "sub-older", "sub-inflight"]


def test_count_submissions_counts_all_or_filtered(store):
    store.record_submission("sub-done", "Accepted", {"product_key": "PK-1", "listing_sku": "SKU-1"})
    store.mark_submission_received("sub-pending")

    assert store.count_submissions() == 2
    assert store.count_submissions(status="Accepted") == 1


def test_store_mark_submission_received_persists_owner_and_payload(store):
    store.mark_submission_received(
        "s1", original_payload={"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"},
        user_id=7)

    state = store.get_submission_state("s1")

    assert state["user_id"] == 7
    assert state["parent_submission_id"] is None
    assert state["original_payload"] == {"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"}


def test_store_get_submission_state_defaults_are_none_for_x_api_key_submissions(store):
    store.mark_submission_received("s2")

    state = store.get_submission_state("s2")

    assert state["user_id"] is None
    assert state["parent_submission_id"] is None
    assert state["original_payload"] is None


def test_store_record_submission_persists_owner_and_parent(store):
    store.record_submission("s3", "Accepted", {"submission_id": "s3", "status": "Accepted"},
                             user_id=7, parent_submission_id="s1")

    state = store.get_submission_state("s3")

    assert state["user_id"] == 7
    assert state["parent_submission_id"] == "s1"


def test_store_mark_submission_ingested_persists_owner_and_parent(store):
    store.mark_submission_ingested("s4", "PRODUCT-KEY", "SKU-1", user_id=7, parent_submission_id="s1")

    state = store.get_submission_state("s4")

    assert state["state"] == "INGESTED"
    assert state["user_id"] == 7
    assert state["parent_submission_id"] == "s1"


def test_store_list_submissions_for_user_groups_root_and_resubmissions_into_one_thread(store):
    store.mark_submission_received("root1", user_id=9)
    store.record_submission("root1", "Clarification required",
                             {"submission_id": "root1", "status": "Clarification required",
                              "missing": ["ModelNumber/UPC"]},
                             user_id=9)
    store.mark_submission_received("resub1", user_id=9, parent_submission_id="root1")
    store.record_submission("resub1", "Accepted", {"submission_id": "resub1", "status": "Accepted"},
                             user_id=9, parent_submission_id="root1")

    threads = store.list_submissions_for_user(9)

    assert len(threads) == 1
    assert threads[0]["root"]["submission_id"] == "root1"
    assert len(threads[0]["resubmissions"]) == 1
    assert threads[0]["resubmissions"][0]["submission_id"] == "resub1"


def test_store_list_submissions_for_user_excludes_other_users(store):
    store.mark_submission_received("mine", user_id=1)
    store.mark_submission_received("theirs", user_id=2)

    threads = store.list_submissions_for_user(1)

    assert len(threads) == 1
    assert threads[0]["root"]["submission_id"] == "mine"


def test_website_hero_column_in_migrations():
    from resale_listing_ai.db import Store

    cols = {(t, c) for (t, c, _typ) in Store._MIGRATIONS}
    assert ("product", "WebsiteHeroImageURL") in cols


def test_build_schema_sql_submission_table_has_ownership_and_threading_columns():
    from resale_listing_ai.db import build_schema_sql

    statements = build_schema_sql("sqlite")
    submission_sql = next(s for s in statements if s.strip().startswith("CREATE TABLE IF NOT EXISTS submission"))

    assert '"user_id"' in submission_sql
    assert '"parent_submission_id"' in submission_sql
    assert '"original_payload_json"' in submission_sql
