"""Postgres-backed store — "Postgres is the source of truth, not CSV" (System
Design §6.1/§11). Table DDL for `product`, `listing`, `submission`, and
`cost_ledger` is generated from config/product_schema.json + listing_schema.json,
so the DB and the Data Contract cannot drift apart. product.csv / listing.csv are
a DERIVED export (Store.export_csv), never the working store.

Every schema field becomes a plain value column (the flattened envelope value);
confidence + source for every field on a row are packed into one `field_meta` JSON
column, so the CSV export (which reads only the value columns) never carries
confidence/source, while a full round-trip through Postgres reconstructs the exact
same {value, confidence, source} envelope dict the pipeline works with.

Two modes, switched by RESALE_LISTING_AI_DB_MODE (env, or .env — see _load_dotenv):
  sqlite   (default) — Python's stdlib sqlite3, in-memory unless
           RESALE_LISTING_AI_SQLITE_PATH is set. No setup, no network: what pytest and
           run_demo.py use automatically. Real ON CONFLICT DO UPDATE (sqlite has
           supported it since 3.24), so tests exercise genuine upsert semantics.
  postgres — a real server via DATABASE_URL (psycopg, lazy-imported so it's only
           required when this mode is actually used). See docker-compose.yml for
           a local instance.
The upsert/idempotency SQL is identical in both dialects; only the JSON/serial
column types and the placeholder style differ (_DIALECTS below).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .records import LISTING_FIELDS, LISTING_SCHEMA, PRODUCT_FIELDS, PRODUCT_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCT_FIELD_TYPES = {f["name"]: f["type"] for f in PRODUCT_SCHEMA["fields"]}
LISTING_FIELD_TYPES = {f["name"]: f["type"] for f in LISTING_SCHEMA["fields"]}


def _field_value(record, name):
    """Unwrap one E-wrapped ({value, confidence, source}) field from a record
    dict, tolerating a missing record/field or a bare value. Returns None when
    absent -- used to project a few product fields onto admin-listing rows."""
    if not record:
        return None
    field = record.get(name)
    if isinstance(field, dict):
        return field.get("value")
    return field

_DIALECTS = {
    # number_type: NUMERIC comes back as Decimal from psycopg but float from
    # sqlite3 — using a floating-point column type in both keeps the value a
    # plain float everywhere (this data is cost/pricing estimates, not exact-
    # decimal accounting), so it's always JSON-serializable and comparable.
    "postgres": {"placeholder": "%s", "json_type": "JSONB", "serial_pk": "id SERIAL PRIMARY KEY",
                 "number_type": "DOUBLE PRECISION"},
    "sqlite": {"placeholder": "?", "json_type": "TEXT", "serial_pk": "id INTEGER PRIMARY KEY AUTOINCREMENT",
               "number_type": "REAL"},
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _quote(identifier):
    return f'"{identifier}"'


def _load_dotenv(path=None):
    """Minimal stdlib-only .env reader: sets only env vars not already set (a
    real exported value always wins), so this is safe to call unconditionally."""
    path = Path(path) if path is not None else REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            value = value.strip(value[0])
        else:
            value = value.split("#", 1)[0].strip()
        os.environ.setdefault(key, value)


def _db_mode():
    return os.environ.get("RESALE_LISTING_AI_DB_MODE", "sqlite")


# ---------------------------------------------------------------------------
# Schema DDL — generated from config/*_schema.json, never hardcoded
# ---------------------------------------------------------------------------

def _column_type(field_type, dialect):
    return _DIALECTS[dialect]["number_type"] if field_type == "number" else "TEXT"


def _record_table_sql(table, fields, field_types, primary_key, dialect):
    json_type = _DIALECTS[dialect]["json_type"]
    columns = [f'{_quote(f)} {_column_type(field_types[f], dialect)}' for f in fields]
    columns.append(f'"field_meta" {json_type}')
    columns.append('"updated_at" TEXT')
    body = ",\n  ".join(columns)
    return (f'CREATE TABLE IF NOT EXISTS {table} (\n  {body},\n  '
            f'PRIMARY KEY ({_quote(primary_key)})\n)')


def _submission_table_sql(dialect):
    """`state` is the per-stage idempotency checkpoint for the two-phase worker
    runtime (RECEIVED implicit/no-row -> INGESTED -> DONE), independent of
    `status`/`result_json` which only carry the terminal outcome. `user_id` is
    null for X-API-Key (server-to-server) submissions -- only /v1/me/* submissions
    have an owner. `parent_submission_id` is null for an original submission,
    else the ROOT submission_id of its resubmission thread -- never the
    immediately-preceding one, so listing a thread is one flat filter."""
    json_type = _DIALECTS[dialect]["json_type"]
    return (
        'CREATE TABLE IF NOT EXISTS submission (\n'
        '  "submission_id" TEXT PRIMARY KEY,\n'
        '  "state" TEXT,\n'
        '  "product_key" TEXT,\n'
        '  "sku" TEXT,\n'
        '  "status" TEXT,\n'
        f'  "result_json" {json_type},\n'
        '  "processed_at" TEXT,\n'
        '  "user_id" INTEGER,\n'
        '  "parent_submission_id" TEXT,\n'
        f'  "original_payload_json" {json_type}\n'
        ')'
    )


def _cost_ledger_table_sql(dialect):
    id_col = _DIALECTS[dialect]["serial_pk"]
    number_type = _DIALECTS[dialect]["number_type"]
    return (
        f'CREATE TABLE IF NOT EXISTS cost_ledger (\n'
        f'  {id_col},\n'
        '  "submission_id" TEXT,\n'
        '  "stage" TEXT,\n'
        '  "service" TEXT,\n'
        f'  "cost_cad" {number_type},\n'
        '  "retry" BOOLEAN,\n'
        '  "input_tokens" INTEGER,\n'
        '  "output_tokens" INTEGER,\n'
        '  "model" TEXT\n'
        ')'
    )


def _user_table_sql(dialect):
    id_col = _DIALECTS[dialect]["serial_pk"]
    return (
        f'CREATE TABLE IF NOT EXISTS app_user (\n'
        f'  {id_col},\n'
        '  "email" TEXT UNIQUE NOT NULL,\n'
        '  "password_hash" TEXT NOT NULL,\n'
        '  "role" TEXT NOT NULL,\n'
        '  "created_at" TEXT NOT NULL\n'
        ')'
    )


def _session_table_sql(dialect):
    return (
        'CREATE TABLE IF NOT EXISTS session (\n'
        '  "session_id" TEXT PRIMARY KEY,\n'
        '  "user_id" INTEGER NOT NULL,\n'
        '  "created_at" TEXT NOT NULL,\n'
        '  "expires_at" TEXT NOT NULL\n'
        ')'
    )


def _admin_config_override_table_sql(dialect):
    return (
        'CREATE TABLE IF NOT EXISTS admin_config_override (\n'
        '  "key" TEXT PRIMARY KEY,\n'
        '  "value_json" TEXT NOT NULL,\n'
        '  "updated_by" INTEGER,\n'
        '  "updated_at" TEXT NOT NULL\n'
        ')'
    )


def build_schema_sql(dialect="postgres"):
    """The 7 CREATE TABLE statements, in dependency order."""
    return [
        _record_table_sql("product", PRODUCT_FIELDS, PRODUCT_FIELD_TYPES, "ProductKey", dialect),
        _record_table_sql("listing", LISTING_FIELDS, LISTING_FIELD_TYPES, "SKU", dialect),
        _submission_table_sql(dialect),
        _cost_ledger_table_sql(dialect),
        _user_table_sql(dialect),
        _session_table_sql(dialect),
        _admin_config_override_table_sql(dialect),
    ]


# ---------------------------------------------------------------------------
# Envelope <-> (flattened values, field_meta) — pure, no DB
# ---------------------------------------------------------------------------

def _flatten_record(record, fields):
    """{field: {value, confidence, source}} -> (values, field_meta), the shape
    every row is stored as: plain value columns + one field_meta JSON blob."""
    values, meta = {}, {}
    for f in fields:
        env = record.get(f) or {"value": None, "confidence": 0.0, "source": "none"}
        values[f] = env.get("value")
        meta[f] = {"confidence": env.get("confidence", 0.0), "source": env.get("source", "none")}
    return values, meta


def _inflate_record(values, field_meta, fields):
    """Inverse of _flatten_record. Any field absent from `values`/`field_meta`
    (never expected in practice, but never assumed) -> a blank envelope, never a
    fabricated one."""
    field_meta = field_meta or {}
    record = {}
    for f in fields:
        meta = field_meta.get(f) or {}
        record[f] = {
            "value": values.get(f),
            "confidence": meta.get("confidence", 0.0),
            "source": meta.get("source", "none"),
        }
    return record


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, conn, dialect):
        self._conn = conn
        self.dialect = dialect
        self._ph = _DIALECTS[dialect]["placeholder"]

    @classmethod
    def connect(cls, database_url=None, mode=None):
        _load_dotenv()
        mode = mode or _db_mode()
        if mode == "postgres":
            database_url = database_url or os.environ.get("DATABASE_URL")
            if not database_url:
                raise RuntimeError(
                    "RESALE_LISTING_AI_DB_MODE=postgres requires DATABASE_URL to be set "
                    "(directly, or via .env — see .env.example)."
                )
            import psycopg

            try:
                conn = psycopg.connect(database_url, autocommit=True)
            except psycopg.OperationalError as e:
                host = urlparse(database_url).hostname
                raise RuntimeError(
                    f"Could not reach the Postgres server at {host!r} "
                    f"(DATABASE_URL). Check the server is up and this machine "
                    f"has network access to it, or fall back to the offline "
                    f"sqlite store by unsetting RESALE_LISTING_AI_DB_MODE. "
                    f"Original error: {e}"
                ) from e
            return cls(conn, "postgres")

        # check_same_thread=False: FastAPI (resale_listing_ai/api.py) runs sync endpoint
        # handlers in a worker-thread pool, but sqlite3 connections are
        # thread-affine by default. sqlite is only ever the offline demo/test
        # fallback (production uses Postgres via psycopg's real connection
        # handling), and callers here don't share a connection across concurrent
        # writers, so this is safe for that scope.
        conn = sqlite3.connect(os.environ.get("RESALE_LISTING_AI_SQLITE_PATH", ":memory:"), check_same_thread=False)
        return cls(conn, "sqlite")

    def _execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def _commit(self):
        if self.dialect == "sqlite":
            self._conn.commit()
        # postgres connections here are opened with autocommit=True

    def close(self):
        self._conn.close()

    # Additive-only, idempotent column migrations for a table that predates them on
    # an already-deployed server (CREATE TABLE IF NOT EXISTS won't add columns to
    # an existing table). No down-migrations/version tracking — deliberately
    # minimal for a pre-production scaffold with no real data yet; replace with a
    # real migration tool (Alembic) before go-live.
    _MIGRATIONS = [("submission", "state", "TEXT"), ("submission", "product_key", "TEXT"),
                    ("submission", "sku", "TEXT"), ("product", "HeroImageSourceURL", "TEXT"),
                    ("product", "WebsiteHeroImageURL", "TEXT"),
                    ("product", "WebsiteHeroPrompt", "TEXT"),
                    ("submission", "user_id", "INTEGER"), ("submission", "parent_submission_id", "TEXT"),
                    ("submission", "original_payload_json", "TEXT"),
                    ("product", "WebsiteHeroReviewRequired", "TEXT"),
                    ("product", "WebsiteHeroRejectedURL", "TEXT"),
                    ("cost_ledger", "input_tokens", "INTEGER"),
                    ("cost_ledger", "output_tokens", "INTEGER"),
                    ("cost_ledger", "model", "TEXT")]

    def ensure_schema(self):
        for stmt in build_schema_sql(self.dialect):
            self._execute(stmt)
        if self.dialect == "postgres":
            for table, column, col_type in self._MIGRATIONS:
                self._execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_quote(column)} {col_type}')
        self._commit()

    def _upsert_sql(self, table, columns, primary_key):
        quoted_cols = ", ".join(_quote(c) for c in columns)
        placeholders = ", ".join([self._ph] * len(columns))
        update_cols = [c for c in columns if c != primary_key]
        set_clause = ", ".join(f'{_quote(c)} = excluded.{_quote(c)}' for c in update_cols)
        return (
            f'INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders}) '
            f'ON CONFLICT ({_quote(primary_key)}) DO UPDATE SET {set_clause}'
        )

    # ---- generic record upsert/get, shared by product + listing ----
    def _upsert_record(self, table, record, fields, primary_key):
        values, meta = _flatten_record(record, fields)
        columns = fields + ["field_meta", "updated_at"]
        row = [values[f] for f in fields] + [json.dumps(meta), _now_iso()]
        self._execute(self._upsert_sql(table, columns, primary_key), row)
        self._commit()

    def _get_record(self, table, primary_key, key_value, fields):
        cur = self._execute(f'SELECT * FROM {table} WHERE {_quote(primary_key)} = {self._ph}', (key_value,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        as_dict = dict(zip(cols, row))
        field_meta = as_dict.get("field_meta")
        field_meta = json.loads(field_meta) if isinstance(field_meta, str) else (field_meta or {})
        values = {f: as_dict.get(f) for f in fields}
        return _inflate_record(values, field_meta, fields)

    # ---- product ----
    def upsert_product(self, product):
        self._upsert_record("product", product, PRODUCT_FIELDS, "ProductKey")

    def get_product(self, product_key):
        return self._get_record("product", "ProductKey", product_key, PRODUCT_FIELDS)

    # ---- listing ----
    def upsert_listing(self, listing):
        self._upsert_record("listing", listing, LISTING_FIELDS, "SKU")

    def get_listing(self, sku):
        return self._get_record("listing", "SKU", sku, LISTING_FIELDS)

    # ---- submission idempotency ----
    def get_submission_result(self, submission_id):
        """Only non-None once the submission has TERMINATED (state DONE) — an
        INGESTED-but-not-yet-imaged row must never look complete here."""
        cur = self._execute(
            f'SELECT result_json FROM submission WHERE submission_id = {self._ph}', (submission_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]

    def get_submission_state(self, submission_id):
        """The per-stage idempotency checkpoint: None if never touched, else
        {'state', 'product_key', 'sku', 'result' (None until DONE), 'user_id'
        (None for X-API-Key submissions), 'parent_submission_id' (None for an
        original submission), 'original_payload' (the {images, notes,
        provided_condition_grade} a /v1/me/submissions or .../answer call was
        made with -- None for X-API-Key submissions, which don't persist it)}."""
        cur = self._execute(
            f'SELECT state, product_key, sku, result_json, user_id, parent_submission_id, '
            f'original_payload_json FROM submission WHERE submission_id = {self._ph}',
            (submission_id,))
        row = cur.fetchone()
        if row is None:
            return None
        state, product_key, sku, result_json, user_id, parent_submission_id, original_payload_json = row
        result = None
        if result_json is not None:
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
        original_payload = None
        if original_payload_json is not None:
            original_payload = (json.loads(original_payload_json)
                                 if isinstance(original_payload_json, str) else original_payload_json)
        return {"state": state, "product_key": product_key, "sku": sku, "result": result,
                "user_id": user_id, "parent_submission_id": parent_submission_id,
                "original_payload": original_payload}

    def mark_submission_received(self, submission_id, original_payload=None, user_id=None,
                                  parent_submission_id=None):
        """Best-effort record that the intake API accepted this submission_id,
        before any worker has touched it. INSERT ... ON CONFLICT DO NOTHING — a
        race where a worker already made progress must never be rolled back."""
        sql = (f'INSERT INTO submission '
               f'(submission_id, state, processed_at, user_id, parent_submission_id, original_payload_json) '
               f'VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}) '
               f'ON CONFLICT (submission_id) DO NOTHING')
        self._execute(sql, (submission_id, "RECEIVED", _now_iso(), user_id, parent_submission_id,
                             json.dumps(original_payload) if original_payload is not None else None))
        self._commit()

    def mark_submission_ingested(self, submission_id, product_key, sku, user_id=None,
                                  parent_submission_id=None):
        """Ingest phase complete, images still pending — the checkpoint a
        redelivered ingest-queue message checks before re-minting a SKU."""
        sql = self._upsert_sql(
            "submission",
            ["submission_id", "state", "product_key", "sku", "status", "result_json", "processed_at",
             "user_id", "parent_submission_id"],
            "submission_id")
        self._execute(sql, (submission_id, "INGESTED", product_key, sku, None, None, _now_iso(),
                             user_id, parent_submission_id))
        self._commit()

    def record_submission(self, submission_id, status, result, user_id=None, parent_submission_id=None):
        """Terminal outcome — Accepted or Accepted-with-unknowns after images, or a
        Rejected/Clarification-required decision at the gate (no images phase needed)."""
        sql = self._upsert_sql(
            "submission",
            ["submission_id", "state", "product_key", "sku", "status", "result_json", "processed_at",
             "user_id", "parent_submission_id"],
            "submission_id")
        self._execute(sql, (
            submission_id, "DONE", result.get("product_key"), result.get("listing_sku"),
            status, json.dumps(result), _now_iso(), user_id, parent_submission_id,
        ))
        self._commit()

    # ---- admin: submissions listing ----
    def list_submissions(self, status=None, limit=50, offset=0):
        # NULLS-portable, dialect-independent tiebreaker: sqlite sorts NULLs
        # last under DESC by default, PostgreSQL sorts NULLs first under DESC
        # by default -- `(processed_at IS NULL), processed_at DESC` forces
        # NULL rows (in-flight submissions) last on both, and `submission_id
        # DESC` gives ties (including multiple NULLs) a stable order.
        if status is not None:
            cur = self._execute(
                f'SELECT submission_id, state, product_key, sku, status, result_json, processed_at '
                f'FROM submission WHERE status = {self._ph} '
                f'ORDER BY (processed_at IS NULL), processed_at DESC, submission_id DESC '
                f'LIMIT {self._ph} OFFSET {self._ph}', (status, limit, offset))
        else:
            cur = self._execute(
                f'SELECT submission_id, state, product_key, sku, status, result_json, processed_at '
                f'FROM submission '
                f'ORDER BY (processed_at IS NULL), processed_at DESC, submission_id DESC '
                f'LIMIT {self._ph} OFFSET {self._ph}',
                (limit, offset))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            result = row.pop("result_json")
            result = json.loads(result) if isinstance(result, str) else result
            row["cost_cad"] = result.get("cost_cad") if result else None
            # Project the AI website-hero review signals so the admin console can
            # flag rows needing review and link to the rejected image, without a
            # second per-row product fetch. Product fields are E-wrapped ({value}).
            product = result.get("product") if result else None
            row["website_hero_review"] = _field_value(product, "WebsiteHeroReviewRequired")
            row["website_hero_rejected_url"] = _field_value(product, "WebsiteHeroRejectedURL")
            row["website_hero_prompt"] = _field_value(product, "WebsiteHeroPrompt")
        return rows

    def count_submissions(self, status=None):
        if status is not None:
            cur = self._execute(f'SELECT COUNT(*) FROM submission WHERE status = {self._ph}', (status,))
        else:
            cur = self._execute('SELECT COUNT(*) FROM submission')
        return cur.fetchone()[0]

    # ---- cost ledger (append-only) ----
    def record_cost_rows(self, rows):
        for r in rows:
            self._execute(
                f'INSERT INTO cost_ledger ("submission_id", "stage", "service", "cost_cad", "retry", '
                f'"input_tokens", "output_tokens", "model") '
                f'VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, '
                f'{self._ph}, {self._ph}, {self._ph})',
                (r["submission_id"], r["stage"], r["service"], r["cost_cad"], bool(r.get("retry", False)),
                 r.get("input_tokens"), r.get("output_tokens"), r.get("model")),
            )
        self._commit()

    def cost_rows(self, submission_id):
        cur = self._execute(
            f'SELECT submission_id, stage, service, cost_cad, retry, '
            f'input_tokens, output_tokens, model FROM cost_ledger '
            f'WHERE submission_id = {self._ph}', (submission_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def count(self, table):
        return self._execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- users ----
    def create_user(self, email, password_hash, role="submitter"):
        self._execute(
            f'INSERT INTO app_user ("email", "password_hash", "role", "created_at") '
            f'VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph})',
            (email, password_hash, role, _now_iso()),
        )
        self._commit()
        return self.get_user_by_email(email)["id"]

    def get_user_by_email(self, email):
        cur = self._execute(f'SELECT * FROM app_user WHERE "email" = {self._ph}', (email,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_user_by_id(self, user_id):
        cur = self._execute(f'SELECT * FROM app_user WHERE "id" = {self._ph}', (user_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def set_user_role(self, user_id, role):
        """Change an existing user's role (e.g. promote to 'admin'). Password and
        every other field are left untouched."""
        self._execute(
            f'UPDATE app_user SET "role" = {self._ph} WHERE "id" = {self._ph}',
            (role, user_id),
        )
        self._commit()

    # ---- admin config overrides ----
    def get_admin_config_overrides(self):
        cur = self._execute('SELECT "key", "value_json", "updated_by", "updated_at" FROM admin_config_override')
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_admin_config_override(self, key):
        cur = self._execute(
            f'SELECT "key", "value_json", "updated_by", "updated_at" FROM admin_config_override '
            f'WHERE "key" = {self._ph}', (key,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def set_admin_config_override(self, key, value_json, updated_by):
        sql = self._upsert_sql("admin_config_override", ["key", "value_json", "updated_by", "updated_at"], "key")
        self._execute(sql, (key, value_json, updated_by, _now_iso()))
        self._commit()

    def delete_admin_config_override(self, key):
        self._execute(f'DELETE FROM admin_config_override WHERE "key" = {self._ph}', (key,))
        self._commit()

    # ---- sessions ----
    def create_session(self, session_id, user_id, expires_at):
        self._execute(
            f'INSERT INTO session ("session_id", "user_id", "created_at", "expires_at") '
            f'VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph})',
            (session_id, user_id, _now_iso(), expires_at),
        )
        self._commit()

    def get_session(self, session_id):
        cur = self._execute(f'SELECT * FROM session WHERE "session_id" = {self._ph}', (session_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def delete_session(self, session_id):
        self._execute(f'DELETE FROM session WHERE "session_id" = {self._ph}', (session_id,))
        self._commit()

    # ---- submitter portal: threads ----
    def list_submissions_for_user(self, user_id):
        """Threads: each root submission (parent_submission_id IS NULL) owned by
        user_id, plus its resubmissions (parent_submission_id = root's id),
        newest-first within each thread. Each row dict has exactly the shape
        submission_status_body(submission_id, state) expects for `state`."""
        cur = self._execute(
            f'SELECT submission_id, state, product_key, sku, result_json, parent_submission_id '
            f'FROM submission WHERE user_id = {self._ph} ORDER BY processed_at DESC',
            (user_id,))
        cols = [d[0] for d in cur.description]
        rows = []
        for raw in cur.fetchall():
            d = dict(zip(cols, raw))
            result_json = d.pop("result_json")
            d["result"] = json.loads(result_json) if isinstance(result_json, str) else result_json
            rows.append(d)

        roots = [r for r in rows if r["parent_submission_id"] is None]
        threads = []
        for root in roots:
            resubmissions = [r for r in rows if r["parent_submission_id"] == root["submission_id"]]
            threads.append({"root": root, "resubmissions": resubmissions})
        return threads

    def get_latest_submission_id_in_thread(self, root_id):
        """The most recently processed submission_id in a thread (the root
        itself, or one of its resubmissions) -- only this one may be answered
        next. Prevents answering an older submission whose own stored outcome
        still shows Clarification required after a newer resubmission has
        already superseded it (which would silently fork a duplicate thread)."""
        cur = self._execute(
            f'SELECT submission_id FROM submission '
            f'WHERE submission_id = {self._ph} OR parent_submission_id = {self._ph} '
            f'ORDER BY processed_at DESC LIMIT 1',
            (root_id, root_id))
        row = cur.fetchone()
        return row[0] if row else None

    # ---- CSV export — DERIVED, never the working store ----
    def export_csv(self, table, fields, path):
        import csv

        cur = self._execute(f'SELECT {", ".join(_quote(f) for f in fields)} FROM {table}')
        rows = cur.fetchall()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(zip(fields, row)))
