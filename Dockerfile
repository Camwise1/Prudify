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
#
# /config and /work are created here rather than left to a volume mount.
# `useradd -d` names a home directory; it does not create one. Without these
# the image has no /config, and because the entrypoint drops to an
# unprivileged user, the first thing the service does -- create its data
# directory -- fails with EACCES at the root of the filesystem. Mounting a
# volume hides the fault, since Docker creates the mount point for you; a
# plain `docker run` with no volumes cannot start at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        tini \
        gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -U -d /config -s /usr/sbin/nologin prudify \
    && mkdir -p /config /work \
    && chown prudify:prudify /config /work

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRUDIFY_DATA_DIR=/config \
    PRUDIFY_HOST=0.0.0.0 \
    PRUDIFY_PORT=8317 \
    # Keep Whisper models on the /config volume so they survive a container rebuild.
    HF_HOME=/config/models \
    XDG_CACHE_HOME=/config/cache \
    # Ownership of files written to the clean library. Match your media
    # share's owner, or Plex/Audiobookshelf cannot manage the results.
    PUID=1000 \
    PGID=1000 \
    UMASK=022

WORKDIR /app

COPY pyproject.toml README.md ./

# Install dependencies alone first. This layer is invalidated only by
# pyproject.toml, so ordinary source edits reuse it instead of re-downloading
# ctranslate2, onnxruntime, av and numpy -- which matters enormously when the
# arm64 build runs under emulation.
RUN mkdir -p backend/prudify \
    && touch backend/prudify/__init__.py \
    && pip install --no-cache-dir ".[whisper]" \
    && pip uninstall -y prudify

COPY backend/ ./backend/
COPY --from=ui /backend/prudify/static/ ./backend/prudify/static/

RUN pip install --no-cache-dir --no-deps .

EXPOSE 8317

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8317/ping',timeout=4).status==200 else 1)"

LABEL org.opencontainers.image.title="Prudify" \
      org.opencontainers.image.description="Self-hosted profanity filtering for audiobook libraries" \
      org.opencontainers.image.source="https://github.com/Camwise1/prudify" \
      org.opencontainers.image.licenses="MIT"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# -g signals the whole process group, so an in-flight ffmpeg child gets
# SIGTERM too rather than running on until the container is torn down.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["prudify", "serve"]
