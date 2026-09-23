# API Integrator Guide

Server-to-server intake: how another system submits items, uploads images, and
retrieves results over the `/v1/*` API. For developers integrating a backend
against Acme Resale — not for the human portal (that uses session cookies; see the
[Submitter Guide](submitter-guide.md)).

> Proof-of-concept build. Known integration limits are called out under
> [Known limitations](#known-limitations); read them before wiring anything up.

## The two API surfaces

The service exposes one API with two auth models on the same origin:

| Surface | Auth | Who |
|---|---|---|
| **Intake** — `/v1/submissions`, `/v1/uploads` | `X-API-Key` header | Server-to-server (this guide) |
| **Portal** — `/v1/auth/*`, `/v1/me/*`, `/v1/admin/*` | session cookie + CSRF | Browsers (the SPA) |

This guide covers the intake surface. The full request/response shape of *every*
endpoint is in the live, generated docs — always the authoritative reference:

- Swagger UI (try-it-out): `/docs`
- ReDoc: `/redoc`
- OpenAPI JSON: `/openapi.json`

## Authentication

Set `RESALE_LISTING_AI_API_KEYS` in the environment to one or more comma-separated keys.
Send one as the `X-API-Key` header on every intake request. A missing or unknown
key is rejected. Keys are shared secrets — treat them like passwords; rotate by
editing the env value and restarting.

## The submission lifecycle

Intake is asynchronous. You enqueue a submission and get an id back immediately
(HTTP `202`); processing happens on a worker. You then either poll for the result
or receive a callback.

```
POST /v1/uploads      -> image refs
POST /v1/submissions  -> { submission_id, status: "RECEIVED" }   (202)
        │
        ▼  (worker processes: identify → gate → price → copy → images)
GET  /v1/submissions/{id}  -> status; terminal states below
```

### 1. Upload images (if you don't already have refs)

Images must exist on the server *before* you reference them. Either upload them
through the API or place them under the server's uploads directory yourself.

```bash
curl -X POST http://localhost:8000/v1/uploads \
  -H "X-API-Key: $KEY" \
  -F "files=@photo1.jpg" -F "files=@photo2.jpg"
# -> 201, returns the stored refs to use as "images" below
```

A submission's `images` are **references, not bytes** — relative paths that must
resolve inside the server's uploads root (`RESALE_LISTING_AI_UPLOADS_DIR`). A reference
that escapes that root is rejected with `422`. This is a deliberate containment
check: the worker reads those paths off disk and forwards them to third-party image
APIs, so an unconstrained path would be a file-exfiltration hole.

### 2. Create the submission

```bash
curl -X POST http://localhost:8000/v1/submissions \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
    "images": ["photo1.jpg", "photo2.jpg"],
    "notes": null,
    "provided_condition_grade": "Open Box",
    "callback_url": "https://your.app/hooks/resale_listing_ai"
  }'
# -> 202 {"submission_id": "…", "status": "RECEIVED"}
```

| Field | Required | Notes |
|---|---|---|
| `images` | yes | Array of refs inside the uploads root (see above). |
| `notes` | no | Free text passed to the pipeline as context. |
| `provided_condition_grade` | no | Kept verbatim, never overwritten by the pipeline. |
| `callback_url` | no | `http(s)` only (else `422`). POSTed to on completion. |

### 3. Get the result

**Poll:**

```bash
curl http://localhost:8000/v1/submissions/$ID -H "X-API-Key: $KEY"
```

**Or set `callback_url`** and receive a completion `POST` instead of polling. (A
callback is best-effort; keep a poll as a fallback for a missed delivery.)

### Terminal outcomes

`status` transitions from `RECEIVED` through processing to one of four terminal
outcomes:

| `status` | Meaning | Record created? |
|---|---|---|
| `Accepted` | Identified with confidence; nothing missing. | Yes — Product + Listing |
| `Accepted with unknowns` | Created, some non-critical fields blank. | Yes |
| `Clarification required` | One or two mandatory facts missing. | No (pending) |
| `Rejected` | More than two mandatory facts missing. | No |

The response also carries the resolved `product_key`, `listing_sku`, cost, and a
`missing` list for the clarification/rejection cases. **Facts are only ever
populated from a real source** — a barcode, OCR text, a priced comparable, a cited
search result — never fabricated to fill a field.

> **Answering a clarification over the API.** The answer/resubmit endpoint
> (`/v1/me/submissions/{id}/answer`) lives on the **portal** surface and needs a
> session cookie, not `X-API-Key`. Server-to-server submissions have no owning user,
> so today there's no `X-API-Key` route to answer a clarification — resubmit as a new
> submission with the missing fact captured in a clearer image, or drive the flow
> through the portal surface. See [Known limitations](#known-limitations).

## Idempotency & redelivery

Processing is idempotent per submission. Re-delivering the same `submission_id`
(for example an at-least-once queue retry) returns the identical prior result —
no re-run, no new SKU, no duplicate cost rows. Two submissions of the *same
product* correctly produce **one Product row and two Listing rows** (dedupe is by
`ProductKey`); each physical unit is its own Listing.

## Errors

Standard HTTP status codes with a JSON `{"detail": "..."}` body:

| Code | When |
|---|---|
| `401` | Missing/invalid `X-API-Key`. |
| `422` | Image ref outside the uploads root; bad `callback_url` scheme; malformed body. |
| `404` | `GET` on an unknown `submission_id`. |

## Known limitations

This is a proof-of-concept; these are disclosed rather than hidden:

- **Image input is local paths, not S3 keys end-to-end.** The API accepts already
  uploaded refs; in a real deployment large images would go to S3 via client-side
  presigned URLs. The image stage still reads local paths only, so true S3-key
  input isn't wired through yet.
- **No `X-API-Key` clarification-answer route.** Answering a clarification is a
  portal (session-cookie) action, because a resubmission attaches to an owning user.
- **The queue message carries the full submission inline** rather than a light
  `{submission_id, s3_prefix}` reference with a separate fetch-by-id step.

For the pipeline internals, provider seams, and queue/worker model behind this API,
see the [Architecture doc](../architecture.md) and
[`docs/DEVELOPING.md`](../DEVELOPING.md).
