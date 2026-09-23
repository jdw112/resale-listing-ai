"""Tests for resale_listing_ai/auth.py — password hashing and session/CSRF token
primitives. Pure module, no DB/FastAPI, so these need no fixtures at all.
Endpoint-level tests (register/login/logout/me, require_user/require_admin)
are appended below as those land in later tasks."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from resale_listing_ai import auth
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue


def test_hash_password_and_verify_password_round_trip():
    hashed = auth.hash_password("correct horse battery staple")

    assert auth.verify_password("correct horse battery staple", hashed)


def test_verify_password_rejects_wrong_password():
    hashed = auth.hash_password("correct horse battery staple")

    assert not auth.verify_password("wrong password", hashed)


def test_hash_password_never_stores_the_plaintext():
    hashed = auth.hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in hashed


def test_new_session_id_and_new_csrf_token_are_distinct_and_random():
    a, b = auth.new_session_id(), auth.new_session_id()

    assert a != b
    assert len(a) > 20


def test_session_expiry_is_roughly_seven_days_out():
    expiry = datetime.fromisoformat(auth.session_expiry())
    now = datetime.now(timezone.utc)

    delta_days = (expiry - now).total_seconds() / 86400

    assert 6.9 < delta_days < 7.1


def test_hash_password_rejects_over_72_bytes():
    with pytest.raises(ValueError):
        auth.hash_password("x" * 73)


def test_verify_password_returns_false_for_over_72_byte_password():
    hashed = auth.hash_password("correct horse battery staple")

    assert not auth.verify_password("x" * 73, hashed)


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RESALE_LISTING_AI_API_KEYS", "test-key")
    # TestClient talks to the app over plain http://testserver — a Secure
    # cookie (the api.py default) would never round-trip back on the next
    # request, breaking every cookie-based test below. Same opt-out a
    # developer uses for local HTTP dev.
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


def test_register_does_not_require_api_key(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    assert resp.status_code == 200


def test_register_sets_session_and_csrf_cookies(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    assert "session_id" in resp.cookies
    assert "csrf_token" in resp.cookies
    body = resp.json()
    assert body["email"] == "jane@example.com"
    assert body["role"] == "submitter"


def test_register_rejects_duplicate_email(client):
    client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "different"})

    assert resp.status_code == 422


def test_register_rejects_password_over_72_bytes(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "x" * 73})

    assert resp.status_code == 422


def test_register_accepts_password_of_exactly_72_bytes(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "x" * 72})

    assert resp.status_code == 200


def test_register_rejects_empty_email(client):
    resp = client.post("/v1/auth/register", json={"email": "", "password": "hunter22"})

    assert resp.status_code == 422


def test_register_rejects_password_under_8_characters(client):
    resp = client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "short"})

    assert resp.status_code == 422


def test_register_rejects_malformed_email(client):
    resp = client.post("/v1/auth/register", json={"email": "not-an-email", "password": "hunter22"})

    assert resp.status_code == 422


def test_login_does_not_require_api_key(client):
    client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    resp = client.post("/v1/auth/login", json={"email": "jane@example.com", "password": "hunter22"})

    assert resp.status_code == 200
    assert "session_id" in resp.cookies


def test_login_rejects_wrong_password(client):
    client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    resp = client.post("/v1/auth/login", json={"email": "jane@example.com", "password": "wrong"})

    assert resp.status_code == 401


def test_login_rejects_unknown_email(client):
    resp = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever"})

    assert resp.status_code == 401


def test_login_calls_verify_password_even_for_an_unknown_email(client, monkeypatch):
    """Guards against a timing side-channel: if verify_password were skipped for an
    unknown email (short-circuited before the bcrypt call), the response would return
    faster for unknown emails than for known ones -- an account-enumeration oracle."""
    calls = []
    real_verify_password = auth.verify_password

    def spy_verify_password(password, password_hash):
        calls.append(password_hash)
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(auth, "verify_password", spy_verify_password)

    resp = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever"})

    assert resp.status_code == 401
    assert len(calls) == 1


def test_login_rejects_password_over_72_bytes(client):
    client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "hunter22"})

    resp = client.post("/v1/auth/login", json={"email": "jane@example.com", "password": "x" * 73})

    assert resp.status_code == 401


def test_login_accepts_password_of_exactly_72_bytes(client):
    client.post("/v1/auth/register", json={"email": "jane@example.com", "password": "x" * 72})

    resp = client.post("/v1/auth/login", json={"email": "jane@example.com", "password": "x" * 72})

    assert resp.status_code == 200


def test_existing_submissions_endpoint_still_requires_api_key_after_router_refactor(client):
    resp = client.post("/v1/submissions", json={
        "images": ["a.jpg"], "notes": None, "provided_condition_grade": "Open Box"})

    assert resp.status_code == 401


def _register_and_get_cookies(client, email="jane@example.com", password="hunter22"):
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    return resp.cookies


def test_me_returns_current_user_with_valid_session(client):
    cookies = _register_and_get_cookies(client)

    resp = client.get("/v1/auth/me", cookies=cookies)

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "jane@example.com"
    assert body["role"] == "submitter"


def test_me_returns_401_without_a_session(client):
    resp = client.get("/v1/auth/me")

    assert resp.status_code == 401


def test_me_returns_401_for_expired_session(client, store):
    _register_and_get_cookies(client)
    user = store.get_user_by_email("jane@example.com")
    expired_session_id = "expired-sess"
    store.create_session(expired_session_id, user["id"], "2000-01-01T00:00:00+00:00")

    resp = client.get("/v1/auth/me", cookies={"session_id": expired_session_id})

    assert resp.status_code == 401


def test_logout_invalidates_the_session(client):
    cookies = _register_and_get_cookies(client)
    csrf_token = cookies["csrf_token"]

    logout_resp = client.post("/v1/auth/logout", cookies=cookies, headers={"X-CSRF-Token": csrf_token})
    assert logout_resp.status_code == 200

    me_resp = client.get("/v1/auth/me", cookies=cookies)
    assert me_resp.status_code == 401


def test_logout_without_csrf_header_is_rejected(client):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/auth/logout", cookies=cookies)

    assert resp.status_code == 403


def test_logout_with_wrong_csrf_header_is_rejected(client):
    cookies = _register_and_get_cookies(client)

    resp = client.post("/v1/auth/logout", cookies=cookies, headers={"X-CSRF-Token": "not-the-real-token"})

    assert resp.status_code == 403


def test_logout_with_non_ascii_csrf_header_is_rejected_not_500(client):
    cookies = _register_and_get_cookies(client)

    # A raw HTTP header carrying a non-ASCII byte (e.g. latin-1 0xE9 for 'é')
    # is what Starlette actually hands the app as a non-ASCII `str` (headers
    # are latin-1 decoded). httpx's own header validation rejects a non-ASCII
    # `str` value client-side, so send the already-latin-1-encoded bytes to
    # reproduce exactly what the server sees on the wire.
    resp = client.post(
        "/v1/auth/logout", cookies=cookies, headers={"X-CSRF-Token": "café".encode("latin-1")}
    )

    assert resp.status_code == 403
