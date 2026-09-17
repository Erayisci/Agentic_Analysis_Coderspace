# Multi-architecture manifest verified against Docker Hub on 2026-09-13.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CRAWL4_AI_BASE_DIRECTORY=/tmp/crawl4ai
WORKDIR /app
# Namespace parent packages deliberately avoid importing any baseline modules.
RUN useradd --uid 10001 --home-dir /tmp/crawler --no-create-home crawler

FROM base AS egress
COPY backend/extensions/web_tools /app/backend/extensions/web_tools
USER 10001:10001
CMD ["python", "-m", "backend.extensions.web_tools.egress"]

FROM base AS crawler-runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY extensions/web_tools/requirements.lock /opt/web-tools/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/web-tools/requirements.lock \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*

FROM crawler-runtime AS crawler
COPY backend/extensions/web_tools /app/backend/extensions/web_tools
COPY extensions/web_tools/tests/test_browser.py /opt/web-tools/tests/test_browser.py
USER 10001:10001
CMD ["python", "-m", "backend.extensions.web_tools.worker"]

# Selected only when document/image capabilities are enabled by the CLI.
FROM crawler-runtime AS crawler-assets
USER root
COPY extensions/web_tools/requirements-assets.lock /opt/web-tools/requirements-assets.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/web-tools/requirements-assets.lock \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-tur fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/web-tools-cache \
    && chown 10001:10001 /opt/web-tools-cache
COPY backend/extensions/web_tools /app/backend/extensions/web_tools
COPY backend/model_clients /app/backend/model_clients
COPY extensions/web_tools/tests/test_browser.py /opt/web-tools/tests/test_browser.py
COPY extensions/web_tools/tests/test_assets_integration.py /opt/web-tools/tests/test_assets_integration.py
USER 10001:10001
CMD ["python", "-m", "backend.extensions.web_tools.worker"]
