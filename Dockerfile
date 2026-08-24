# syntax=docker/dockerfile:1

# ---------- stage 1: build the web UI ----------
FROM node:20-alpine AS ui

WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./
# vite.config.js emits to ../backend/prudify/static, which from /ui resolves
# to /backend/prudify/static.
RUN mkdir -p /backend/prudify/static && npm run build


# ---------- stage 2: runtime ----------
FROM python:3.11-slim AS runtime

# ffmpeg does the audio work; libgomp is required by CTranslate2 (faster-whisper).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRUDIFY_DATA_DIR=/config \
    PRUDIFY_HOST=0.0.0.0 \
    PRUDIFY_PORT=8317 \
    # Keep Whisper models on the /config volume so they survive a container rebuild.
    HF_HOME=/config/models \
    XDG_CACHE_HOME=/config/cache \
    OMP_NUM_THREADS=4

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend/ ./backend/
COPY --from=ui /backend/prudify/static/ ./backend/prudify/static/

RUN pip install --no-cache-dir ".[whisper]"

VOLUME ["/config"]
EXPOSE 8317

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8317/ping',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["prudify", "serve"]
