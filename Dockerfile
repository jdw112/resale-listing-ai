# syntax=docker/dockerfile:1

# --- stage 1: build the frontend into static assets ---
FROM node:24-slim AS web
WORKDIR /web
# Install deps first so this layer caches unless the lockfile changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /web/dist

# --- stage 2: runtime (Debian slim, NOT alpine: glibc manylinux wheels for
#     psycopg / Pillow / pillow-heif / bcrypt install prebuilt) ---
FROM python:3.13-slim-bookworm AS runtime

# Keep Python lean and predictable in a container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System libraries the real adapters need at runtime:
#   libzbar0      — the zbar shared library pyzbar loads for barcode_read
#   tesseract-ocr — the OCR binary pytesseract shells out to for ocr_read
# (Both are skipped in stub mode, but required for live provider processing.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libzbar0 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for layer caching. slim + these deps installs entirely from
# prebuilt wheels, so no compiler/build toolchain is needed.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code + config, then the built frontend from stage 1.
COPY resale_listing_ai/ ./resale_listing_ai/
COPY config/ ./config/
COPY --from=web /web/dist ./frontend/dist
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/uploads /app/out \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Entrypoint runs the optional admin bootstrap (RESALE_LISTING_AI_BOOTSTRAP_ADMIN=1) then
# execs the command below. The worker runs from this SAME image with a different
# command (see docker-compose.yml): python -m resale_listing_ai.worker --loop
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "resale_listing_ai.api:app", "--host", "0.0.0.0", "--port", "8000"]
