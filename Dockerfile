# syntax=docker/dockerfile:1
# Archy backend — Python 3.11 slim image.
# Used by Render (render.yaml) and any other container host (Fly.io, Railway, etc.).
FROM python:3.11-slim AS base

# System deps: psycopg2-binary bundles libpq, but we add a few basics.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY pyproject.toml run.py ./
RUN pip install --no-cache-dir -e ".[dev]"

# Copy the application code
COPY archy/ ./archy/
COPY tests/ ./tests/

# Create the data directory (used only if ARCHY_DB_URL is unset — desktop mode)
RUN mkdir -p /data

# Default env: production-style online mode.
# Override ARCHY_DB_URL, GEMINI_API_KEY, ARCHY_ALLOWED_ORIGINS via the host.
ENV ARCHY_HOST=0.0.0.0 \
    ARCHY_PORT=8000 \
    ARCHY_MODE=online

EXPOSE 8000

# Health check — hits /keep-alive every 5 min, 3 retries before unhealthy.
HEALTHCHECK --interval=300s --timeout=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/keep-alive || exit 1

# Run via uvicorn directly (no need for run.py / CLI in container).
CMD ["uvicorn", "archy.server:app", "--host", "0.0.0.0", "--port", "8000"]
