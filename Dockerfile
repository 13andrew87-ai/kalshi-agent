# Kalshi Agent — production web image (dashboard + paper/sim agent).
# Build:  docker build -t kalshi-agent .
# Run:    see DEPLOY.md (env KALSHI_WEB_PASSWORD is REQUIRED for the gate)
#
# This image contains NO secrets: the private key and password are injected at
# runtime (env vars / mounted state volume).  The gate fails closed when
# KALSHI_WEB_PASSWORD is unset.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code + non-secret configs only (see .dockerignore — state/ is NOT copied)
COPY kalshi_agent ./kalshi_agent
COPY config.example.yaml config-live.yaml ./
COPY fly-entrypoint.sh ./
COPY README.md ./

# Run as an unprivileged user; state lives in a volume (persists credentials)
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/state \
    && chown -R app:app /app
USER app

RUN chmod +x /app/fly-entrypoint.sh

EXPOSE 8080
VOLUME ["/app/state"]

# Liveness: /healthz is intentionally unauthenticated
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).read()" || exit 1

# Production: account console + permanent live agent (watchdog, shared volume).
# Agent only arms itself if the Kalshi credentials exist on the volume.
CMD ["/app/fly-entrypoint.sh"]
