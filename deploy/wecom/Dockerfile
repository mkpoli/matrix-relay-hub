# Build from the repository root:
#   docker build -f deploy/wecom/Dockerfile -t matrix-relay-hub/wecom-bridge:local .
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir aiohttp==3.14.5 cryptography==50.0.2 pillow==13.0.0

WORKDIR /app
COPY --chown=10001:10001 wecom/src/wecom_bridge/ /app/src/wecom_bridge/

USER 10001:10001
ENTRYPOINT ["python", "-m", "wecom_bridge"]
CMD ["run", "--config", "/data/config.toml"]
