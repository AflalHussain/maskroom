# PII masking web UI + gateway API.
#   docker build -t maskroom .
#   docker run -p 8080:8080 -e PII_TOKEN_SALT=... maskroom
FROM python:3.12-slim

# Tesseract is needed to redact scanned PDFs; PyMuPDF ships its own MuPDF.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies and the spaCy model first so code edits don't invalidate this layer.
COPY requirements.txt pyproject.toml README.md ./
COPY maskroom ./maskroom
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0 \
    && pip install --no-cache-dir --no-deps . \
    && python -m spacy download en_core_web_lg

COPY webui ./webui
# Only the manifest: /ext/update.xml derives the extension id from its key.
COPY extension/manifest.json ./extension/manifest.json

# Set PII_TOKEN_SALT as a platform secret; never bake it into the image.
# Runtime files (uploads, audit copies, ext/*.crx, the default SQLite db) live
# under /data; mount a volume there. Set MASKROOM_DATABASE_URL for Postgres.
ENV HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1 MASKROOM_DATA_DIR=/data
VOLUME /data
EXPOSE 8080

# One worker: the spaCy model is loaded once per process (~1 GB RAM each).
# Threads serve concurrent requests; the session store's engine lock serializes
# analysis. Set MASKROOM_AUTH_MODE=oidc with the MASKROOM_OIDC_* values for sign-on,
# SESSION_TTL_HOURS for vault expiry and AUDIT_TTL_DAYS for audit retention.
# graceful-timeout matches --timeout so a redeploy lets an in-flight OCR job finish.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 300 --graceful-timeout 300 --bind 0.0.0.0:${PORT} webui.app:app"]
