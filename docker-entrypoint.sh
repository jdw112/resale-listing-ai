#!/bin/sh
# Container entrypoint. When RESALE_LISTING_AI_BOOTSTRAP_ADMIN=1 (set on the `app` service
# only, so the worker doesn't race it), idempotently ensure the bootstrap admin
# exists before starting the real process. A bootstrap failure is logged but does
# NOT block startup — the app still comes up, and the admin can be created later
# with `resale_listing_ai.admin create-admin`.
set -e

# .env may carry AWS_PROFILE (for host dev); inside the container boto3 uses the
# explicit AWS_ACCESS_KEY_ID/SECRET against localstack, so a profile name here only
# breaks it (ProfileNotFound). Unset it rather than trying to blank it in compose
# (an empty AWS_PROFILE is itself an error).
unset AWS_PROFILE

# Idempotently ensure the image-storage S3 bucket exists (localstack in dev).
# Runs on both app and worker (create is idempotent); non-fatal — the pipeline
# falls back to local disk if it can't be reached.
echo "[entrypoint] ensuring S3 bucket ${RESALE_LISTING_AI_S3_BUCKET:-resale-listing-ai-dev} on ${AWS_ENDPOINT_URL:-default endpoint}"
python -m resale_listing_ai.bootstrap_s3 || echo "[entrypoint] S3 bucket bootstrap failed — continuing (local-disk fallback)"

if [ "$RESALE_LISTING_AI_BOOTSTRAP_ADMIN" = "1" ]; then
  echo "[entrypoint] ensuring bootstrap admin (RESALE_LISTING_AI_ADMIN_EMAIL=${RESALE_LISTING_AI_ADMIN_EMAIL:-unset})"
  python -m resale_listing_ai.admin ensure-admin || echo "[entrypoint] admin bootstrap failed — continuing without it"
fi

exec "$@"
