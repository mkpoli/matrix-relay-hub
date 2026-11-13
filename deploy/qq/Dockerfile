# Build from the repository root:
#   docker build -f deploy/qq/Dockerfile -t matrix-relay-hub/qq-bridge:local .
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir aiohttp==3.14.5 pillow==13.0.0

WORKDIR /app
COPY --chown=10001:10001 qq/src/qq_bridge/ /app/src/qq_bridge/

USER 10001:10001
ENTRYPOINT ["python", "-m", "qq_bridge"]
CMD ["run", "--config", "/data/config.toml"]
