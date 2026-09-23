"""Tests for resale_listing_ai/api.py — the intake API (System Design §5.1), via FastAPI's
in-process TestClient (no real sockets/ports). The API only enqueues + reports
state; a worker (resale_listing_ai/worker.py) is run explicitly in these tests to
simulate the decoupled queue/worker plane, same as production."""

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai import worker
from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue

API_HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    # setenv, not delenv: _load_dotenv() uses os.environ.setdefault(), which
    # would silently re-populate a deleted var from a real repo .env (e.g.
    # RESALE_LISTING_AI_DB_MODE=postgres uncommented for live-demo use).
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_API_KEYS", "test-key")


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture
def queues():
    ingest_dlq = LocalQueue("ingest-dlq")
    ingest = LocalQueue("ingest-queue", dlq=ingest_dlq)
    image_dlq = LocalQueue("image-dlq")
    image = LocalQueue("image-queue", dlq=image_dlq)
    return {"ingest": ingest, "ingest_dlq": ingest_dlq, "image": image, "image_dlq": image_dlq}


@pytest.fixture
def client(store, queues):
    from resale_listing_ai.api import create_app

    return TestClient(create_app(store=store, queues=queues))


def test_post_submission_returns_202_with_submission_id_and_received_status(client):
    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["submission_id"]
    assert body["status"] == "RECEIVED"


def test_post_submission_enqueues_onto_ingest_queue(client, queues):
    client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert len(queues["ingest"]) == 1


def test_post_submission_rejects_absolute_image_path(client, queues, store):
    """An absolute path would let image_process() read any file the server
    process can see (e.g. .env, SSH keys) and forward its bytes to the
    third-party PhotoRoom API — the endpoint must reject it before it ever
    reaches a worker."""
    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["/etc/passwd"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 422
    assert len(queues["ingest"]) == 0
    assert store.count("submission") == 0


def test_post_submission_rejects_path_traversal_image_ref(client, queues):
    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["../../../etc/passwd"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 422
    assert len(queues["ingest"]) == 0


def test_post_submission_rejects_non_http_callback_url(client, queues):
    """A file:// (or any non-http(s)) callback_url would let the worker's completion
    POST target an arbitrary local resource or cloud metadata endpoint from inside
    the server process — the endpoint must reject it before it ever reaches a worker."""
    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box",
        "callback_url": "file:///etc/passwd"})

    assert resp.status_code == 422
    assert len(queues["ingest"]) == 0


def test_post_submission_accepts_https_callback_url(client):
    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box",
        "callback_url": "https://example.com/webhook"})

    assert resp.status_code == 202


def test_get_unknown_submission_returns_404(client):
    resp = client.get("/v1/submissions/no-such-id", headers=API_HEADERS)

    assert resp.status_code == 404


def test_get_submission_reflects_received_before_any_worker_runs(client):
    created = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"}).json()

    resp = client.get(f"/v1/submissions/{created['submission_id']}", headers=API_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["state"] == "RECEIVED"


def test_get_submission_reflects_published_after_worker_processes_it(client, store, queues):
    created = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box", "hint": "nest-doorbell"}).json()
    sid = created["submission_id"]

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.get(f"/v1/submissions/{sid}", headers=API_HEADERS)
    body = resp.json()
    assert body["state"] == "PUBLISHED"
    assert body["outcome"] == "Accepted"
    assert body["product_key"]
    assert body["listing_sku"]
    assert body["cost_cad"] > 0


def test_get_submission_reflects_rejected_outcome(client, store, queues):
    created = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair", "hint": "mystery"}).json()
    sid = created["submission_id"]

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.get(f"/v1/submissions/{sid}", headers=API_HEADERS)
    body = resp.json()
    assert body["state"] in ("REJECTED", "CLARIFICATION_REQUIRED")
    assert body["missing"]


def test_post_submission_without_api_key_returns_401(client):
    resp = client.post("/v1/submissions", json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 401


def test_post_submission_with_wrong_api_key_returns_401(client):
    resp = client.post("/v1/submissions", headers={"X-API-Key": "not-the-right-key"}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 401


def test_get_submission_includes_full_product_and_listing_on_accepted(client, store, queues):
    created = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box", "hint": "nest-doorbell"}).json()
    sid = created["submission_id"]

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.get(f"/v1/submissions/{sid}", headers=API_HEADERS)
    body = resp.json()
    assert body["product"]["Brand"]["value"] == "Acme"
    assert body["product"]["Brand"]["confidence"] == 0.92
    assert body["listing"]["SKU"]["value"] == body["listing_sku"]


def test_get_submission_omits_product_and_listing_on_rejected(client, store, queues):
    created = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair", "hint": "mystery"}).json()
    sid = created["submission_id"]

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.get(f"/v1/submissions/{sid}", headers=API_HEADERS)
    body = resp.json()
    assert body["product"] is None
    assert body["listing"] is None
