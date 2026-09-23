"""Tests for resale_listing_ai/api.py's /v1/me/* routes — the submitter portal's
backend surface. Offline pattern (sqlite Store, LocalQueue), same as
test_api.py/test_auth.py. This file accumulates across this plan's tasks."""

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai import worker
from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_API_KEYS", "test-key")
    # TestClient talks to the app over plain http://testserver — a Secure
    # cookie (the api.py default) would never round-trip back on the next
    # request, breaking every cookie-based test below. Same opt-out
    # test_auth.py uses for local HTTP dev.
    monkeypatch.setenv("RESALE_LISTING_AI_COOKIE_SECURE", "0")


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


def _register_and_get_cookies(client, email="jane@example.com", password="hunter22"):
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    return resp.cookies


def test_me_uploads_requires_a_session(client):
    resp = client.post("/v1/me/uploads", files=[("files", ("photo1.jpg", b"bytes", "image/jpeg"))])

    assert resp.status_code == 401


def test_me_uploads_returns_a_ref_for_an_authenticated_user(client):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/me/uploads", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]},
                        files=[("files", ("photo1.jpg", b"fake-jpeg-bytes", "image/jpeg"))])

    assert resp.status_code == 201
    uploads = resp.json()["uploads"]
    assert len(uploads) == 1
    assert uploads[0]["filename"] == "photo1.jpg"


def test_condition_grades_requires_a_session(client):
    resp = client.get("/v1/me/config/condition-grades")

    assert resp.status_code == 401


def test_condition_grades_returns_the_provided_grades_list(client):
    cookies = _register_and_get_cookies(client)

    resp = client.get("/v1/me/config/condition-grades", cookies=cookies)

    assert resp.status_code == 200
    grades = resp.json()["grades"]
    assert "Open Box" in grades
    assert "New" in grades


def test_create_me_submission_requires_a_session(client):
    resp = client.post("/v1/me/submissions", json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 401


def test_create_me_submission_returns_202_and_enqueues(client, queues):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/me/submissions", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 202
    assert resp.json()["status"] == "RECEIVED"
    assert len(queues["ingest"]) == 1


def test_create_me_submission_rejects_invalid_image_ref(client):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/me/submissions", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["/etc/passwd"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 422


def test_list_me_submissions_requires_a_session(client):
    resp = client.get("/v1/me/submissions")

    assert resp.status_code == 401


def test_list_me_submissions_shows_only_the_callers_own_submissions(client, store, queues):
    cookies_a = _register_and_get_cookies(client, "a@example.com")
    cookies_b = _register_and_get_cookies(client, "b@example.com")

    client.post("/v1/me/submissions", cookies=cookies_a,
                headers={"X-CSRF-Token": cookies_a["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box", "hint": "nest-doorbell"})
    client.post("/v1/me/submissions", cookies=cookies_b,
                headers={"X-CSRF-Token": cookies_b["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    resp_a = client.get("/v1/me/submissions", cookies=cookies_a)

    assert resp_a.status_code == 200
    threads = resp_a.json()["threads"]
    assert len(threads) == 1


def test_list_me_submissions_reflects_published_after_worker_processes_it(client, store, queues):
    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box",
        "hint": "nest-doorbell"}).json()

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.get("/v1/me/submissions", cookies=cookies)
    thread = resp.json()["threads"][0]
    assert thread["root"]["submission_id"] == created["submission_id"]
    assert thread["root"]["state"] == "PUBLISHED"
    assert thread["root"]["outcome"] == "Accepted"


def test_get_me_submission_returns_403_for_a_non_owned_submission(client):
    cookies_a = _register_and_get_cookies(client, "a@example.com")
    cookies_b = _register_and_get_cookies(client, "b@example.com")
    created = client.post("/v1/me/submissions", cookies=cookies_a,
                           headers={"X-CSRF-Token": cookies_a["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"}).json()

    resp = client.get(f"/v1/me/submissions/{created['submission_id']}", cookies=cookies_b)

    assert resp.status_code == 403


def test_get_me_submission_returns_404_for_an_unknown_submission(client):
    cookies = _register_and_get_cookies(client)

    resp = client.get("/v1/me/submissions/no-such-id", cookies=cookies)

    assert resp.status_code == 404


def test_answer_requires_at_least_one_field(client, store, queues):
    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.post(f"/v1/me/submissions/{created['submission_id']}/answer", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]}, json={})

    assert resp.status_code == 422


def test_answer_rejects_a_non_clarification_required_submission(client, store, queues):
    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box",
        "hint": "nest-doorbell"}).json()
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.post(f"/v1/me/submissions/{created['submission_id']}/answer", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]},
                        json={"field_overrides": {"ModelNumber": "XYZ"}})

    assert resp.status_code == 409


def test_answer_returns_403_for_a_non_owned_submission(client, store, queues):
    cookies_a = _register_and_get_cookies(client, "a@example.com")
    cookies_b = _register_and_get_cookies(client, "b@example.com")
    created = client.post("/v1/me/submissions", cookies=cookies_a,
                           headers={"X-CSRF-Token": cookies_a["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resp = client.post(f"/v1/me/submissions/{created['submission_id']}/answer", cookies=cookies_b,
                        headers={"X-CSRF-Token": cookies_b["csrf_token"]},
                        json={"field_overrides": {"ModelNumber": "XYZ"}})

    assert resp.status_code == 403


def test_answer_full_round_trip_creates_a_linked_resubmission(client, store, queues, monkeypatch):
    """The full flow this feature exists for: submit -> worker -> Clarification
    required -> answer -> worker -> the resubmission is linked to the root via
    parent_submission_id, and shows up as one thread in the list."""
    from resale_listing_ai import adapters
    from resale_listing_ai.envelope import E, NULL

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.9, "vision"),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]}, json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    root_id = created["submission_id"]
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    root_detail = client.get(f"/v1/me/submissions/{root_id}", cookies=cookies).json()
    assert root_detail["outcome"] == "Clarification required"
    assert root_detail["missing"] == ["ModelNumber/UPC"]

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)  # same identity on the resubmission
    answer_resp = client.post(f"/v1/me/submissions/{root_id}/answer", cookies=cookies,
                               headers={"X-CSRF-Token": cookies["csrf_token"]},
                               json={"field_overrides": {"ModelNumber": "XYZ123"}})
    assert answer_resp.status_code == 202
    resub_id = answer_resp.json()["submission_id"]
    assert resub_id != root_id

    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resub_state = store.get_submission_state(resub_id)
    assert resub_state["parent_submission_id"] == root_id
    assert resub_state["result"]["status"] in ("Accepted", "Accepted with unknowns")

    threads = client.get("/v1/me/submissions", cookies=cookies).json()["threads"]
    assert len(threads) == 1
    assert threads[0]["root"]["submission_id"] == root_id
    assert len(threads[0]["resubmissions"]) == 1
    assert threads[0]["resubmissions"][0]["submission_id"] == resub_id


def test_create_me_submission_rejects_an_unknown_condition_grade(client):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/me/submissions", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]},
                        json={"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Mint Condition"})

    assert resp.status_code == 422


def test_answer_rejects_a_field_not_in_the_missing_list(client, store, queues, monkeypatch):
    from resale_listing_ai import adapters
    from resale_listing_ai.envelope import E, NULL

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": NULL(),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": E("XYZ123", 0.9, "vision"),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]},
                           json={"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    detail = client.get(f"/v1/me/submissions/{created['submission_id']}", cookies=cookies).json()
    assert detail["outcome"] == "Clarification required"
    assert detail["missing"] == ["Brand"]

    resp = client.post(f"/v1/me/submissions/{created['submission_id']}/answer", cookies=cookies,
                        headers={"X-CSRF-Token": cookies["csrf_token"]},
                        json={"field_overrides": {"PricingAnchorPriceCAD": "9.99"}})

    assert resp.status_code == 422


def test_answering_a_superseded_submission_is_rejected(client, store, queues, monkeypatch):
    """Guards against duplicate resubmissions: once a newer submission exists in
    the thread, the older one can no longer be answered, even if its own stored
    outcome still shows Clarification required."""
    from resale_listing_ai import adapters
    from resale_listing_ai.envelope import E, NULL

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        return {
            "Brand": E("Acme", 0.9, "vision"),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]},
                           json={"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    root_id = created["submission_id"]
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    csrf = cookies["csrf_token"]
    answer1 = client.post(f"/v1/me/submissions/{root_id}/answer", cookies=cookies,
                           headers={"X-CSRF-Token": csrf},
                           json={"field_overrides": {"ModelNumber/UPC": "XYZ123"}})
    assert answer1.status_code == 202
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    answer2 = client.post(f"/v1/me/submissions/{root_id}/answer", cookies=cookies,
                           headers={"X-CSRF-Token": csrf},
                           json={"field_overrides": {"ModelNumber/UPC": "ABC999"}})
    assert answer2.status_code == 409


def test_field_overrides_accumulate_across_answer_generations(client, store, queues, monkeypatch):
    """A second clarification round must not drop the first round's override --
    otherwise gate() re-asks the first field forever once a second field is
    also missing."""
    from resale_listing_ai import adapters
    from resale_listing_ai.envelope import E, NULL

    call_count = {"n": 0}

    def fake_vision_identify(submission, ledger=None, ocr_text=None, config=None):
        call_count["n"] += 1
        return {
            "Brand": NULL(),
            "ProductName": E("Widget", 0.9, "vision"),
            "InternalCategory": E("Tools", 0.9, "vision"),
            "ModelNumber": NULL(),
        }

    monkeypatch.setattr(adapters, "vision_identify", fake_vision_identify)
    monkeypatch.setattr(adapters, "barcode_read", lambda submission: NULL())

    cookies = _register_and_get_cookies(client)
    created = client.post("/v1/me/submissions", cookies=cookies,
                           headers={"X-CSRF-Token": cookies["csrf_token"]},
                           json={"images": ["a.jpg"], "notes": None, "provided_condition_grade": "Used - Fair"}).json()
    root_id = created["submission_id"]
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    csrf = cookies["csrf_token"]
    # Round 1: answer only the anchor. Brand is still missing, so this stays
    # Clarification required -- but the ModelNumber/UPC override must survive
    # into round 2, not get dropped.
    answer1 = client.post(f"/v1/me/submissions/{root_id}/answer", cookies=cookies,
                           headers={"X-CSRF-Token": csrf},
                           json={"field_overrides": {"ModelNumber/UPC": "XYZ123"}})
    assert answer1.status_code == 202
    resub1_id = answer1.json()["submission_id"]
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resub1_detail = client.get(f"/v1/me/submissions/{resub1_id}", cookies=cookies).json()
    assert resub1_detail["outcome"] == "Clarification required"
    assert resub1_detail["missing"] == ["Brand"]  # ModelNumber/UPC is satisfied now

    # Round 2: answer the remaining field. If round 1's override was dropped,
    # gate() would still see no confident ModelNumber/UPC and reject again.
    answer2 = client.post(f"/v1/me/submissions/{resub1_id}/answer", cookies=cookies,
                           headers={"X-CSRF-Token": csrf},
                           json={"field_overrides": {"Brand": "Acme"}})
    assert answer2.status_code == 202
    resub2_id = answer2.json()["submission_id"]
    worker.run_once(store, queues, ledger=CostLedger(), sleep=lambda s: None)

    resub2_state = store.get_submission_state(resub2_id)
    assert resub2_state["result"]["status"] in ("Accepted", "Accepted with unknowns")
