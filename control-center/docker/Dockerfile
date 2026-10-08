# syntax=docker/dockerfile:1
#
# Hermes Agent Control Center — single image, both processes.
#
# Design notes that matter:
#   * Hermes is installed from the upstream Git source as an editable dependency.
#     It is NOT vendored: rebuild with --build-arg HERMES_REF=main to advance it.
#   * The image runs as an unprivileged user. Hermes writes to HERMES_HOME only.
#   * No secret is baked in. Everything sensitive arrives at runtime through the
#     environment or the encrypted vault inside CC_HOME.
#   * The UI is built in a throwaway stage and copied in as static files.
#
# BUILD CONTEXT: control-center/ — never the repository root. From inside docker/:
#   docker build -f docker/Dockerfile ..
# Everything COPYed below is therefore relative to control-center/. Making it the
# context root is also what makes docker read control-center/.dockerignore instead
# of upstream Hermes' (which excludes every *.md and knows nothing of our state).

# ----------------------------------------------------------------- prebuilt UI
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ------------------------------------------------------------------ application
FROM python:3.11-slim AS app

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HERMES_HOME=/data/hermes \
    CC_HOME=/data/cc \
    CC_HOST=0.0.0.0 \
    CC_PORT=8080 \
    CC_HERMES_SOURCE=/opt/hermes-agent \
    CC_RUNTIME_MODE=dashboard \
    CC_HERMES_PORT=9119 \
    CC_CHAT_BRIDGE_PORT=9120

# git: cloning and updating the upstream project
# curl/ca-certificates: health probes and provider HTTPS
# build-essential: some provider SDKs still ship sdists
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git
ARG HERMES_REF=main
RUN git clone --depth 1 --branch "${HERMES_REF}" "${HERMES_REPO}" /opt/hermes-agent \
 && rm -rf /opt/hermes-agent/.git/shallow.lock

WORKDIR /opt/control-center

# Hermes itself, then the Control Center, in two layers so a backend change does
# not re-download the agent's dependency tree.
RUN pip install --upgrade pip setuptools wheel
RUN pip install -e "/opt/hermes-agent[web]" || pip install -e /opt/hermes-agent

COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/ ./backend/
RUN pip install -e ./backend

COPY --from=ui /ui/build /opt/control-center/frontend/build

# Unprivileged runtime user. UID 1000 keeps bind-mounted volumes usable on a
# typical Linux host; override with --user if your host differs.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin hermes \
 && mkdir -p /data/hermes /data/cc \
 && chown -R hermes:hermes /data /opt/control-center /opt/hermes-agent
USER hermes

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${CC_PORT}/health" || exit 1

# The Control Center owns the Hermes process; when it exits, both stop.
CMD ["python", "-m", "app"]
