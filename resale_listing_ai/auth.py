"""Auth primitives — password hashing and session/CSRF token generation.
Pure, no FastAPI/Store coupling, so these are directly unit-testable. The
FastAPI dependencies that use them (require_user, require_admin) live in
api.py, alongside the Store instance they need to look up a session
against -- sessions are server-side (a `session` table row per login),
not a stateless JWT, so they're actually revocable, which matters since
one of the two roles this guards is "admin"."""

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

SESSION_COOKIE = "session_id"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SESSION_DURATION_DAYS = 7


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password):
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    if len(password.encode("utf-8")) > 72:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def new_session_id():
    return secrets.token_urlsafe(32)


def new_csrf_token():
    return secrets.token_urlsafe(32)


def session_expiry():
    return (datetime.now(timezone.utc) + timedelta(days=SESSION_DURATION_DAYS)).isoformat()
