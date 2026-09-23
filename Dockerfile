# Backend image: the FastAPI service plus a lakehouse built from the
# committed regulator archives (no network needed at build time -- see
# CLAUDE.md "Raw data" for why those archives, not the build output, are the
# source of truth).
FROM python:3.11-slim

WORKDIR /app

# Playwright's browser install needs these; installing them once here (not
# per `playwright install --with-deps`) keeps the apt layer cacheable
# separately from the Python dependency layer below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a source change below doesn't invalidate this layer.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev,ingest]"

# The browser binary Playwright needs for JS-rendered pages (backend/tools/
# browser_render.py); --with-deps also installs its OS-level libraries. A
# missing browser degrades read_url to a plain fetch rather than failing, but
# baking it in here means that path is never silently degraded on demo day.
RUN playwright install --with-deps chromium

# The committed source-of-truth archives the build reads (all offline).
COPY bddk_aylik_bulten ./bddk_aylik_bulten
COPY bddk_haftalik_bulten ./bddk_haftalik_bulten
COPY bddk_finturk ./bddk_finturk
COPY evds ./evds
COPY riskmerkezi_sectoral ./riskmerkezi_sectoral
COPY scripts ./scripts

# Build the lakehouse once, into the image, so a container starts instantly
# with no first-request build step and no writable volume required.
RUN python -m backend.ingestion.bddk_bulletin --from-cache \
    && python -m backend.lakehouse.build

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
