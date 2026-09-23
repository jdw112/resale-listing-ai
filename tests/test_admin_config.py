"""Tests for GET/PUT/DELETE /v1/admin/config[/{key}] (resale_listing_ai/api.py)."""

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
    return {"session_id": session_id, "csrf_token": "test-csrf-token"}, {"X-CSRF-Token": "test-csrf-token"}


def _submitter_cookies(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})
    return resp.cookies


def test_get_admin_config_returns_401_for_anonymous(client):
    resp = client.get("/v1/admin/config")
    assert resp.status_code == 401


def test_get_admin_config_returns_403_for_a_submitter(client):
    cookies = _submitter_cookies(client)
    resp = client.get("/v1/admin/config", cookies=cookies)
    assert resp.status_code == 403


def test_get_admin_config_returns_all_eleven_keys_with_no_overrides(client, store):
    cookies, _ = _admin_cookies(store)

    resp = client.get("/v1/admin/config", cookies=cookies)

    assert resp.status_code == 200
    entries = resp.json()["config"]
    assert len(entries) == 11
    assert all(e["is_override"] is False for e in entries)
    assert all(e["value"] == e["default"] for e in entries)
    keys = {e["key"] for e in entries}
    assert "gate.threshold" in keys
    assert "provider_stack.vision_provider" in keys
    assert "business.oversized_ups_ca" in keys


def test_get_admin_config_reports_env_var_default_not_file_default(client, store, monkeypatch):
    # No admin override for provider_stack.vision_provider is set. With
    # RESALE_LISTING_AI_VISION_PROVIDER set in the deployment environment, the real
    # runtime seam (adapters._vision_provider) picks it up ahead of the file
    # default -- GET /v1/admin/config must report that same effective value,
    # not silently fall back to the file's "openai".
    monkeypatch.setenv("RESALE_LISTING_AI_VISION_PROVIDER", "gemini")
    cookies, _ = _admin_cookies(store)

    resp = client.get("/v1/admin/config", cookies=cookies)

    assert resp.status_code == 200
    entry = next(e for e in resp.json()["config"] if e["key"] == "provider_stack.vision_provider")
    assert entry["value"] == "gemini"
    assert entry["default"] == "gemini"
    assert entry["is_override"] is False


def test_put_admin_config_sets_an_override(client, store):
    cookies, headers = _admin_cookies(store)

    resp = client.put("/v1/admin/config/gate.threshold", json={"value": 0.8},
                       cookies=cookies, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == 0.8
    assert body["is_override"] is True

    get_resp = client.get("/v1/admin/config", cookies=cookies)
    entry = next(e for e in get_resp.json()["config"] if e["key"] == "gate.threshold")
    assert entry["value"] == 0.8
    assert entry["is_override"] is True
    assert entry["updated_by"] is not None


def test_put_admin_config_rejects_invalid_value_with_422(client, store):
    cookies, headers = _admin_cookies(store)

    resp = client.put("/v1/admin/config/gate.threshold", json={"value": 5.0},
                       cookies=cookies, headers=headers)

    assert resp.status_code == 422


def test_put_admin_config_rejects_unknown_key_with_404(client, store):
    cookies, headers = _admin_cookies(store)

    resp = client.put("/v1/admin/config/not.a.real.key", json={"value": "x"},
                       cookies=cookies, headers=headers)

    assert resp.status_code == 404


def test_put_admin_config_requires_csrf_header(client, store):
    cookies, _ = _admin_cookies(store)

    resp = client.put("/v1/admin/config/gate.threshold", json={"value": 0.8}, cookies=cookies)

    assert resp.status_code == 403


def test_put_admin_config_returns_403_for_a_submitter(client, store):
    submitter_cookies = _submitter_cookies(client)
    csrf = submitter_cookies["csrf_token"]

    resp = client.put("/v1/admin/config/gate.threshold", json={"value": 0.8},
                       cookies=submitter_cookies, headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 403


def test_delete_admin_config_clears_an_override(client, store):
    cookies, headers = _admin_cookies(store)
    client.put("/v1/admin/config/gate.threshold", json={"value": 0.8}, cookies=cookies, headers=headers)

    resp = client.delete("/v1/admin/config/gate.threshold", cookies=cookies, headers=headers)
    assert resp.status_code == 204

    get_resp = client.get("/v1/admin/config", cookies=cookies)
    entry = next(e for e in get_resp.json()["config"] if e["key"] == "gate.threshold")
    assert entry["is_override"] is False
    assert entry["value"] == entry["default"]


def test_delete_admin_config_rejects_unknown_key_with_404(client, store):
    cookies, headers = _admin_cookies(store)

    resp = client.delete("/v1/admin/config/not.a.real.key", cookies=cookies, headers=headers)

    assert resp.status_code == 404
