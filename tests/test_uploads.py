"""Tests for resale_listing_ai/api.py's POST /v1/uploads — the direct image-upload
endpoint external callers use to get image bytes onto disk before referencing
them in POST /v1/submissions. Offline throughout, same pattern as test_api.py."""

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai import api as api_module
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue

API_HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
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
def client(store, queues, tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "UPLOADS_ROOT", tmp_path)
    return TestClient(api_module.create_app(store=store, queues=queues))


def test_upload_single_file_returns_ref(client, tmp_path):
    resp = client.post("/v1/uploads", headers=API_HEADERS,
                        files=[("files", ("photo1.jpg", b"fake-jpeg-bytes", "image/jpeg"))])

    assert resp.status_code == 201
    uploads = resp.json()["uploads"]
    assert len(uploads) == 1
    assert uploads[0]["filename"] == "photo1.jpg"
    ref = uploads[0]["ref"]
    assert ref.endswith(".jpg")
    assert (tmp_path / ref).read_bytes() == b"fake-jpeg-bytes"


def test_upload_multiple_files_in_one_call_returns_one_ref_each(client, tmp_path):
    resp = client.post("/v1/uploads", headers=API_HEADERS, files=[
        ("files", ("a.jpg", b"aaa", "image/jpeg")),
        ("files", ("b.jpg", b"bbb", "image/jpeg")),
    ])

    assert resp.status_code == 201
    uploads = resp.json()["uploads"]
    assert len(uploads) == 2
    refs = {u["ref"] for u in uploads}
    assert len(refs) == 2  # distinct generated names, no collision


def test_uploaded_ref_is_usable_in_a_submission(client, queues):
    upload_resp = client.post("/v1/uploads", headers=API_HEADERS,
                               files=[("files", ("photo1.jpg", b"fake-jpeg-bytes", "image/jpeg"))])
    ref = upload_resp.json()["uploads"][0]["ref"]

    resp = client.post("/v1/submissions", headers=API_HEADERS, json={
        "images": [ref], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 202
    assert len(queues["ingest"]) == 1


def test_oversize_file_rejected_with_422(client, monkeypatch):
    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 10)

    resp = client.post("/v1/uploads", headers=API_HEADERS,
                        files=[("files", ("big.jpg", b"x" * 11, "image/jpeg"))])

    assert resp.status_code == 422
    assert "big.jpg" in resp.json()["detail"]


def test_very_long_extension_does_not_500(client, tmp_path):
    """Regression test: a filename with a very long 'extension' (e.g. no real dot,
    or a huge trailing segment) must not blow past filesystem filename limits and
    surface as an unhandled 500 — the suffix used in the generated ref is truncated."""
    long_name = "a." + "x" * 400

    resp = client.post("/v1/uploads", headers=API_HEADERS,
                        files=[("files", (long_name, b"fake-bytes", "image/jpeg"))])

    assert resp.status_code == 201
    uploads = resp.json()["uploads"]
    assert len(uploads) == 1
    ref = uploads[0]["ref"]
    assert len(ref) < 64
    assert (tmp_path / ref).read_bytes() == b"fake-bytes"


def test_batch_upload_with_oversized_file_orphans_none(client, tmp_path, monkeypatch):
    """Regression test: multi-file upload where a later file is oversized should
    result in 422 AND zero files written to disk (not just the oversized one)."""
    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 100)

    resp = client.post("/v1/uploads", headers=API_HEADERS, files=[
        ("files", ("small1.jpg", b"aaa", "image/jpeg")),      # 3 bytes, passes
        ("files", ("oversized.jpg", b"x" * 150, "image/jpeg")),  # 150 bytes, fails
        ("files", ("small2.jpg", b"bbb", "image/jpeg")),      # 3 bytes, would pass
    ])

    assert resp.status_code == 422
    assert "oversized.jpg" in resp.json()["detail"]

    # Verify zero files were written (not just the oversized one — none of them)
    written_files = list(tmp_path.glob("*"))
    assert len(written_files) == 0, f"Expected zero files written, but found {len(written_files)}: {written_files}"
