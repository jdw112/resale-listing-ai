"""Tests for the single-container SPA static mount in resale_listing_ai/api.py.

When a built frontend (dist/index.html) is present, create_app serves it: API
routes still win, unknown paths fall back to the SPA shell so client-side routes
survive a hard refresh, real asset files are served, and path traversal outside
dist/ is refused. When no build is present, the mount is a no-op (local dev uses
the Vite server; every other test in this suite builds nothing and is unaffected).
"""

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_COOKIE_SECURE", "0")


@pytest.fixture
def dist(tmp_path, monkeypatch):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>resale_listing_ai spa</title>")
    (d / "assets" / "app.js").write_text("console.log('built asset')")
    monkeypatch.setenv("RESALE_LISTING_AI_FRONTEND_DIST", str(d))
    return d


@pytest.fixture
def client(dist):
    from resale_listing_ai.api import create_app

    store = Store.connect()
    store.ensure_schema()
    queues = {
        "ingest": LocalQueue("ingest-queue"),
        "ingest_dlq": LocalQueue("ingest-dlq"),
        "image": LocalQueue("image-queue"),
        "image_dlq": LocalQueue("image-dlq"),
    }
    yield TestClient(create_app(store=store, queues=queues))
    store.close()


def test_root_serves_the_spa_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "resale_listing_ai spa" in resp.text


def test_unknown_client_route_falls_back_to_index(client):
    # A deep client-side route (hard refresh) must return the SPA shell, not 404.
    resp = client.get("/submissions/2026-08-23-abc")
    assert resp.status_code == 200
    assert "resale_listing_ai spa" in resp.text


def test_real_built_asset_is_served(client):
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "built asset" in resp.text


def test_api_routes_still_win_over_spa_fallback(client):
    # /v1/auth/me is registered before the catch-all, so it must 401 (not
    # authenticated) rather than being swallowed by the index.html fallback.
    resp = client.get("/v1/auth/me")
    assert resp.status_code == 401
    assert "resale_listing_ai spa" not in resp.text


def test_path_traversal_outside_dist_is_refused(client):
    # A traversal that escapes dist/ must not leak files; it falls back to index.
    resp = client.get("/../../../etc/passwd")
    assert resp.status_code == 200
    assert "resale_listing_ai spa" in resp.text
    assert "root:" not in resp.text
