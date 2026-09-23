"""Intake API — System Design §3.1/§5.1: "Accepts a submission, stores originals,
enqueues a job, returns a submission_id." A small FastAPI service.

`images` here are already-uploaded references (local paths for this scaffold's
demo/tests; S3 keys in a real deployment) — per the doc, "large images upload
directly to S3 via presigned URLs so the API stays light and the payload is just
keys + metadata," so this API never proxies file bytes. Original-image S3 storage
itself happens inside adapters.image_process during the images stage, same as
every other real adapter call in this codebase.

The API does NOT run the worker inline — it only enqueues and reports state, the
same decoupling the System Design's queueing plane exists for. Run a worker
separately (`python -m resale_listing_ai.worker --run-once` for a local demo) to actually
process what's queued.
"""

import json
import os
import secrets
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, FastAPI, File, HTTPException, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from . import adapters, admin_config, auth, json_repair, pipeline
from .db import Store
from .envelope import THRESHOLD as _GATE_THRESHOLD_DEFAULT
from .pipeline import CORE_FIELDS as _GATE_CORE_FIELDS_DEFAULT, MANDATORY as _GATE_MANDATORY_DEFAULT
from .queue import connect_queues
from .submission_view import submission_status_body

REPO_ROOT = Path(__file__).resolve().parents[1]
# adapters.image_process() reads every submitted image reference straight off the
# local filesystem (Path(path).read_bytes()) — real S3-key fetch isn't wired up yet
# (see README "Known simplifications"). Until it is, every reference here must
# resolve inside this directory, or a submission could make a worker read and
# forward (to a third-party image API, then to S3) an arbitrary file the server
# process can see — .env, SSH keys, etc.
UPLOADS_ROOT = Path(os.environ.get("RESALE_LISTING_AI_UPLOADS_DIR", REPO_ROOT / "uploads")).resolve()

CONFIG_DIR = REPO_ROOT / "config"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB per file — the one abuse guard, given rate limiting is out of scope

_CONDITION_SCALE = json.loads((REPO_ROOT / "config" / "condition_scale.json").read_text())

# A fixed dummy hash so login always pays the bcrypt cost, even for an unknown
# email — otherwise an unknown-email request short-circuits before ever calling
# bcrypt while a known-email request always does, a timing side channel that
# lets an attacker enumerate valid emails.
_DUMMY_PASSWORD_HASH = auth.hash_password("dummy-password-for-constant-time-comparison")


def _authorized_keys():
    raw = os.environ.get("RESALE_LISTING_AI_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _cookie_secure():
    # Session/CSRF cookies default to Secure (HTTPS-only). A developer running
    # the frontend dev server against a local HTTP backend (or a test client
    # talking over plain http://testserver) sets RESALE_LISTING_AI_COOKIE_SECURE=0 to
    # disable it — an explicit opt-out, not an insecure default. Read per-call
    # (like _authorized_keys() above), not cached at import time, so tests can
    # toggle it via monkeypatch regardless of module import order.
    return os.environ.get("RESALE_LISTING_AI_COOKIE_SECURE", "1") != "0"


def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    if x_api_key not in _authorized_keys():
        raise HTTPException(status_code=401, detail="missing or invalid API key")


class SubmissionIn(BaseModel):
    images: list[str]
    notes: str | None = None
    provided_condition_grade: str
    hint: str | None = None  # demo-only: lets the offline stub adapters recognize a known item
    callback_url: str | None = None


class MeSubmissionIn(BaseModel):
    images: list[str]
    notes: str | None = None
    provided_condition_grade: str | None = None
    hint: str | None = None  # demo-only: lets the offline stub adapters recognize a known item


class AnswerIn(BaseModel):
    field_overrides: dict[str, str] = {}
    images: list[str] = []


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: str
    password: str


class ConfigOverrideIn(BaseModel):
    value: Any


def _new_submission_id():
    return f"{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"


def _admin_config_defaults():
    categories = json.loads((CONFIG_DIR / "categories.json").read_text())["categories"]
    condition_scale_raw = json.loads((CONFIG_DIR / "condition_scale.json").read_text())
    condition_scale = {
        "provided_grades": condition_scale_raw["provided_grades"],
        "ai_suggested_grades": condition_scale_raw["ai_suggested_grades"],
    }
    oversized = json.loads((CONFIG_DIR / "oversized_ups_ca.json").read_text())["rules"]["oversized_if_any"]
    return {
        # config=None routes through the real provider-selector seams
        # (env var, then file default) so this reports the actual effective
        # default -- not just the file's value, which env vars can override.
        "provider_stack.vision_provider": adapters._vision_provider(None),
        "provider_stack.copy_provider": adapters._copy_provider(None),
        "provider_stack.web_search_provider": adapters._web_search_provider(None),
        "provider_stack.image_process_provider": adapters._image_process_provider(None),
        "provider_stack.json_repair_provider": json_repair._json_repair_provider(None),
        "gate.threshold": _GATE_THRESHOLD_DEFAULT,
        "gate.mandatory_fields": _GATE_MANDATORY_DEFAULT,
        "gate.core_fields": _GATE_CORE_FIELDS_DEFAULT,
        "business.categories": categories,
        "business.condition_scale": condition_scale,
        "business.oversized_ups_ca": oversized,
    }


def _is_within_uploads_root(image_ref):
    if not image_ref or Path(image_ref).is_absolute():
        return False
    resolved = (UPLOADS_ROOT / image_ref).resolve()
    return resolved == UPLOADS_ROOT or UPLOADS_ROOT in resolved.parents


def _write_uploads(files):
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)

    for f in files:
        if (f.size or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=422, detail=f"file too large: {f.filename!r}")

    uploads = []
    for f in files:
        suffix = Path(f.filename or "").suffix[:16]
        ref = f"{uuid.uuid4().hex}{suffix}"
        with open(UPLOADS_ROOT / ref, "wb") as out:
            shutil.copyfileobj(f.file, out)
        uploads.append({"ref": ref, "filename": f.filename})
    return {"uploads": uploads}


def create_app(store=None, queues=None):
    """store/queues are injectable for tests; real serving (`uvicorn
    resale_listing_ai.api:app`) connects both from the environment/.env (see db.py's and
    queue.py's RESALE_LISTING_AI_DB_MODE / RESALE_LISTING_AI_QUEUE_MODE)."""
    store = store or Store.connect()
    store.ensure_schema()
    queues = queues or connect_queues()

    app = FastAPI(title="Acme Resale Intake API")

    def _login_response(user_id, response):
        user = store.get_user_by_id(user_id)
        session_id = auth.new_session_id()
        store.create_session(session_id, user_id, auth.session_expiry())
        csrf_token = auth.new_csrf_token()
        response.set_cookie(auth.SESSION_COOKIE, session_id, httponly=True, samesite="lax", secure=_cookie_secure())
        response.set_cookie(auth.CSRF_COOKIE, csrf_token, httponly=False, samesite="lax", secure=_cookie_secure())
        return {"id": user["id"], "email": user["email"], "role": user["role"]}

    def require_user(request: Request, session_id: str | None = Cookie(None, alias=auth.SESSION_COOKIE)):
        if session_id is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        session = store.get_session(session_id)
        if session is None or session["expires_at"] < auth.now_iso():
            raise HTTPException(status_code=401, detail="not authenticated")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            csrf_cookie = request.cookies.get(auth.CSRF_COOKIE)
            csrf_header = request.headers.get(auth.CSRF_HEADER)
            if (
                not csrf_cookie
                or not csrf_header
                or not secrets.compare_digest(csrf_cookie.encode("utf-8"), csrf_header.encode("utf-8"))
            ):
                raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
        user = store.get_user_by_id(session["user_id"])
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    def require_admin(user: dict = Depends(require_user)):
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return user

    # ---- server-to-server routes: unchanged behavior, now on their own router ----
    v1_router = APIRouter(dependencies=[Depends(require_api_key)])

    @v1_router.post("/v1/submissions", status_code=202)
    def create_submission(payload: SubmissionIn):
        for ref in payload.images:
            if not _is_within_uploads_root(ref):
                raise HTTPException(status_code=422, detail=f"invalid image reference: {ref!r}")

        if payload.callback_url and urlparse(payload.callback_url).scheme not in ("http", "https"):
            raise HTTPException(status_code=422, detail=f"invalid callback_url: {payload.callback_url!r}")

        submission_id = _new_submission_id()
        submission = {
            "submission_id": submission_id, "images": payload.images, "notes": payload.notes,
            "provided_condition_grade": payload.provided_condition_grade,
        }
        if payload.hint:
            submission["hint"] = payload.hint
        if payload.callback_url:
            submission["callback_url"] = payload.callback_url

        store.mark_submission_received(submission_id)
        queues["ingest"].send({"submission": submission})

        return {"submission_id": submission_id, "status": "RECEIVED"}

    @v1_router.get("/v1/submissions/{submission_id}")
    def get_submission(submission_id: str):
        state = store.get_submission_state(submission_id)
        if state is None:
            raise HTTPException(status_code=404, detail="submission not found")
        return submission_status_body(submission_id, state)

    @v1_router.post("/v1/uploads", status_code=201)
    def create_uploads(files: list[UploadFile] = File(...)):
        return _write_uploads(files)

    # ---- per-user routes: no X-API-Key, session-cookie auth instead ----
    auth_router = APIRouter()

    @auth_router.post("/v1/auth/register")
    def register(payload: RegisterIn, response: Response):
        if store.get_user_by_email(payload.email) is not None:
            raise HTTPException(status_code=422, detail=f"email already registered: {payload.email!r}")
        if len(payload.password.encode("utf-8")) > 72:
            raise HTTPException(status_code=422, detail="password must be at most 72 bytes")
        password_hash = auth.hash_password(payload.password)
        user_id = store.create_user(payload.email, password_hash, role="submitter")
        return _login_response(user_id, response)

    @auth_router.post("/v1/auth/login")
    def login(payload: LoginIn, response: Response):
        user = store.get_user_by_email(payload.email)
        password_hash = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
        # Evaluated unconditionally (not short-circuited by `user is None or ...`) so an
        # unknown email pays the same bcrypt cost as a known one -- otherwise the response
        # timing itself becomes an account-enumeration oracle.
        password_ok = auth.verify_password(payload.password, password_hash)
        if user is None or not password_ok:
            raise HTTPException(status_code=401, detail="invalid email or password")
        return _login_response(user["id"], response)

    @auth_router.get("/v1/auth/me")
    def me(user: dict = Depends(require_user)):
        return {"id": user["id"], "email": user["email"], "role": user["role"]}

    @auth_router.post("/v1/auth/logout")
    def logout(response: Response, user: dict = Depends(require_user),
               session_id: str | None = Cookie(None, alias=auth.SESSION_COOKIE)):
        store.delete_session(session_id)
        response.delete_cookie(auth.SESSION_COOKIE)
        response.delete_cookie(auth.CSRF_COOKIE)
        return {"status": "logged_out"}

    # ---- admin routes: session-cookie auth + require_admin, like auth_router ----
    admin_router = APIRouter()

    @admin_router.get("/v1/admin/config")
    def get_admin_config(user: dict = Depends(require_admin)):
        defaults = _admin_config_defaults()
        overrides = {row["key"]: row for row in store.get_admin_config_overrides()}
        entries = []
        for key in admin_config.KEYS:
            override = overrides.get(key)
            if override is not None:
                entries.append({
                    "key": key, "value": json.loads(override["value_json"]), "default": defaults[key],
                    "is_override": True, "updated_by": override["updated_by"], "updated_at": override["updated_at"],
                })
            else:
                entries.append({
                    "key": key, "value": defaults[key], "default": defaults[key],
                    "is_override": False, "updated_by": None, "updated_at": None,
                })
        return {"config": entries}

    @admin_router.put("/v1/admin/config/{key}")
    def put_admin_config(key: str, payload: ConfigOverrideIn, user: dict = Depends(require_admin)):
        if key not in admin_config.KEYS:
            raise HTTPException(status_code=404, detail=f"unknown config key: {key!r}")
        try:
            admin_config.validate_value(key, payload.value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        store.set_admin_config_override(key, json.dumps(payload.value), user["id"])
        row = store.get_admin_config_override(key)
        return {
            "key": key, "value": json.loads(row["value_json"]), "is_override": True,
            "updated_by": row["updated_by"], "updated_at": row["updated_at"],
        }

    @admin_router.delete("/v1/admin/config/{key}", status_code=204)
    def delete_admin_config(key: str, user: dict = Depends(require_admin)):
        if key not in admin_config.KEYS:
            raise HTTPException(status_code=404, detail=f"unknown config key: {key!r}")
        store.delete_admin_config_override(key)

    @admin_router.get("/v1/admin/submissions")
    def get_admin_submissions(status: str | None = None, limit: int = 50, offset: int = 0,
                               user: dict = Depends(require_admin)):
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        submissions = store.list_submissions(status=status, limit=limit, offset=offset)
        total = store.count_submissions(status=status)
        return {"submissions": submissions, "total": total}

    @admin_router.get("/v1/admin/hero-image/{submission_id}")
    def get_admin_hero_image(submission_id: str, user: dict = Depends(require_admin)):
        """Stream the QC-rejected AI website-hero image for one submission so the
        admin review link can display it. The image URL is read from the
        submission's product record (never from the request), and the bytes are
        proxied from whichever storage backend (local disk / S3) holds them so the
        browser needs no direct storage access."""
        state = store.get_submission_state(submission_id)
        if state is None or not state.get("product_key"):
            raise HTTPException(status_code=404, detail="submission not found")
        product = store.get_product(state["product_key"])
        rejected = product["WebsiteHeroRejectedURL"]["value"] if product else None
        if not rejected:
            raise HTTPException(status_code=404, detail="no rejected hero image for this submission")
        try:
            data, content_type = adapters._read_object_url(rejected)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="rejected hero image is no longer available")
        return Response(content=data, media_type=content_type)

    app.include_router(admin_router)

    # ---- submitter portal routes: require_user (session cookie), never X-API-Key ----
    me_router = APIRouter(dependencies=[Depends(require_user)])

    @me_router.post("/v1/me/uploads", status_code=201)
    def create_me_uploads(files: list[UploadFile] = File(...)):
        return _write_uploads(files)

    @me_router.get("/v1/me/config/condition-grades")
    def condition_grades():
        return {"grades": _CONDITION_SCALE["provided_grades"]}

    @me_router.post("/v1/me/submissions", status_code=202)
    def create_me_submission(payload: MeSubmissionIn, user: dict = Depends(require_user)):
        for ref in payload.images:
            if not _is_within_uploads_root(ref):
                raise HTTPException(status_code=422, detail=f"invalid image reference: {ref!r}")

        if payload.provided_condition_grade and payload.provided_condition_grade not in _CONDITION_SCALE["provided_grades"]:
            raise HTTPException(status_code=422,
                                 detail=f"invalid provided_condition_grade: {payload.provided_condition_grade!r}")

        submission_id = _new_submission_id()
        submission = {
            "submission_id": submission_id, "images": payload.images, "notes": payload.notes,
            "provided_condition_grade": payload.provided_condition_grade, "user_id": user["id"],
        }
        if payload.hint:
            submission["hint"] = payload.hint
        store.mark_submission_received(
            submission_id,
            original_payload={"images": payload.images, "notes": payload.notes,
                               "provided_condition_grade": payload.provided_condition_grade},
            user_id=user["id"],
        )
        queues["ingest"].send({"submission": submission})

        return {"submission_id": submission_id, "status": "RECEIVED"}

    @me_router.get("/v1/me/submissions")
    def list_me_submissions(user: dict = Depends(require_user)):
        threads = store.list_submissions_for_user(user["id"])
        return {
            "threads": [
                {
                    "root": submission_status_body(t["root"]["submission_id"], t["root"]),
                    "resubmissions": [
                        submission_status_body(r["submission_id"], r) for r in t["resubmissions"]
                    ],
                }
                for t in threads
            ]
        }

    @me_router.get("/v1/me/submissions/{submission_id}")
    def get_me_submission(submission_id: str, user: dict = Depends(require_user)):
        state = store.get_submission_state(submission_id)
        if state is None:
            raise HTTPException(status_code=404, detail="submission not found")
        if state["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="not your submission")
        return submission_status_body(submission_id, state)

    @me_router.post("/v1/me/submissions/{submission_id}/answer", status_code=202)
    def answer_me_submission(submission_id: str, payload: AnswerIn, user: dict = Depends(require_user)):
        if not payload.field_overrides and not payload.images:
            raise HTTPException(status_code=422,
                                 detail="provide at least one field_overrides entry or one image")

        state = store.get_submission_state(submission_id)
        if state is None:
            raise HTTPException(status_code=404, detail="submission not found")
        if state["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="not your submission")

        root_id = state["parent_submission_id"] or submission_id
        latest_id = store.get_latest_submission_id_in_thread(root_id)
        if latest_id != submission_id:
            raise HTTPException(status_code=409, detail="a newer submission already exists in this thread")
        if (state["state"] != "DONE" or state["result"] is None
                or state["result"].get("status") != "Clarification required"):
            raise HTTPException(status_code=409, detail="submission is not awaiting clarification")

        missing = (state["result"] or {}).get("missing", [])
        allowed_fields = pipeline.answerable_fields(missing)
        for field in payload.field_overrides:
            if field not in allowed_fields:
                raise HTTPException(status_code=422, detail=f"field not currently askable: {field!r}")

        for ref in payload.images:
            if not _is_within_uploads_root(ref):
                raise HTTPException(status_code=422, detail=f"invalid image reference: {ref!r}")

        original = state["original_payload"] or {}
        new_images = list(original.get("images", []))
        for ref in payload.images:
            if ref not in new_images:
                new_images.append(ref)
        merged_overrides = {**original.get("field_overrides", {}), **payload.field_overrides}

        new_submission_id = _new_submission_id()
        new_submission = {
            "submission_id": new_submission_id,
            "images": new_images,
            "notes": original.get("notes"),
            "provided_condition_grade": original.get("provided_condition_grade"),
            "field_overrides": merged_overrides,
            "user_id": user["id"],
            "parent_submission_id": root_id,
        }
        store.mark_submission_received(
            new_submission_id,
            original_payload={"images": new_images, "notes": original.get("notes"),
                               "provided_condition_grade": original.get("provided_condition_grade"),
                               "field_overrides": merged_overrides},
            user_id=user["id"], parent_submission_id=root_id,
        )
        queues["ingest"].send({"submission": new_submission})

        return {"submission_id": new_submission_id, "status": "RECEIVED"}

    app.include_router(v1_router)
    app.include_router(auth_router)
    app.include_router(me_router)

    # ---- serve the built frontend (single-container deploy) ----
    # Registered LAST so every API route above is matched first; the SPA
    # catch-all only handles what the routers didn't claim. Skipped entirely
    # when no build is present (local dev uses the Vite server; tests never
    # build), so this is a no-op unless `npm run build` has produced dist/.
    dist_dir = Path(os.environ.get("RESALE_LISTING_AI_FRONTEND_DIST", REPO_ROOT / "frontend" / "dist"))
    index_html = dist_dir / "index.html"
    if index_html.is_file():
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            # Serve a real file when one exists (favicon, etc.), otherwise the
            # SPA shell so client-side routes survive a hard refresh. API 404s
            # never reach here — those routers are registered before this.
            candidate = (dist_dir / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist_dir.resolve()):
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()
