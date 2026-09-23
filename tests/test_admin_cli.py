"""Tests for resale_listing_ai/admin.py — the admin bootstrap CLI. Offline sqlite store."""

import pytest

from resale_listing_ai import auth
from resale_listing_ai.admin import _ensure
from resale_listing_ai.db import Store


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


def test_creates_a_new_admin_with_a_usable_password(store):
    msg = _ensure(store, "admin@example.com", "s3cret-pw", allow_prompt=False)
    assert "created" in msg
    u = store.get_user_by_email("admin@example.com")
    assert u["role"] == "admin"
    assert auth.verify_password("s3cret-pw", u["password_hash"])


def test_promotes_an_existing_submitter_without_touching_password(store):
    # A normal signup: submitter with their own password.
    store.create_user("jane@example.com", auth.hash_password("orig-pw"), role="submitter")

    msg = _ensure(store, "jane@example.com", "ignored-different-pw", allow_prompt=False)
    assert "promoted" in msg
    u = store.get_user_by_email("jane@example.com")
    assert u["role"] == "admin"
    # Password is unchanged — the promote path must never reset it.
    assert auth.verify_password("orig-pw", u["password_hash"])
    assert not auth.verify_password("ignored-different-pw", u["password_hash"])


def test_existing_admin_is_left_unchanged(store):
    store.create_user("boss@example.com", auth.hash_password("orig-pw"), role="admin")
    msg = _ensure(store, "boss@example.com", None, allow_prompt=False)
    assert "unchanged" in msg
    assert store.get_user_by_email("boss@example.com")["role"] == "admin"


def test_new_admin_without_password_is_refused(store):
    with pytest.raises(SystemExit):
        _ensure(store, "nopass@example.com", None, allow_prompt=False)
    assert store.get_user_by_email("nopass@example.com") is None


def test_idempotent_across_repeated_runs(store):
    _ensure(store, "admin@example.com", "pw1", allow_prompt=False)
    msg = _ensure(store, "admin@example.com", "pw2", allow_prompt=False)
    assert "unchanged" in msg
    # Still exactly one row, password from the original create.
    u = store.get_user_by_email("admin@example.com")
    assert auth.verify_password("pw1", u["password_hash"])
