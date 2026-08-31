# Cloud CPU engine for NoSpeaky.
# Multi-stage-ish simple image: ffmpeg + faster-whisper.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NOSPEAKY_HOST=0.0.0.0 \
    NOSPEAKY_PORT=8788 \
    NOSPEAKY_MODEL=Systran/faster-whisper-small \
    NOSPEAKY_DEVICE=cpu \
    NOSPEAKY_COMPUTE_TYPE=int8 \
    NOSPEAKY_MAX_UPLOAD_MB=200 \
    NOSPEAKY_MAX_DURATION_SEC=600 \
    NOSPEAKY_JOB_LIMIT_PER_HOUR=8

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      curl \
      unzip \
      ca-certificates \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY engine/requirements-cloud.txt /app/engine/requirements-cloud.txt
RUN pip install --upgrade pip && pip install -r /app/engine/requirements-cloud.txt

COPY engine /app/engine

# non-root
RUN useradd -m -u 10001 appuser && mkdir -p /app/engine/data/jobs && chown -R appuser:appuser /app
USER appuser

EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8788/health || exit 1

CMD ["uvicorn", "engine.server:app", "--host", "0.0.0.0", "--port", "8788", "--app-dir", "/app"]
