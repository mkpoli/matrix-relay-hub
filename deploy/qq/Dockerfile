# Build from the existing QQ bridge checkout:
#   docker build -f deploy/qq/Dockerfile -t matrix-relay-hub/qq-bridge:local \
#     "$QQ_BRIDGE_SOURCE"
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir aiohttp==3.14.5

WORKDIR /app
COPY --chown=10001:10001 src/ /app/src/

USER 10001:10001
ENTRYPOINT ["python", "-m", "qq_bridge"]
CMD ["run", "--config", "/data/config.toml"]
