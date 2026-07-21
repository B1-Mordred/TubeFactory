FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --gid 10003 editorial && useradd --uid 10003 --gid editorial --create-home editorial
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg=7:5.1.9-0+deb12u1 && rm -rf /var/lib/apt/lists/*
COPY packages/core /app/packages/core
COPY packages/ai-gateway /app/packages/ai-gateway
COPY services/editorial-worker/pyproject.toml /tmp/editorial-worker/pyproject.toml
COPY services/editorial-worker/src/editorial_worker/__init__.py /tmp/editorial-worker/src/editorial_worker/__init__.py
RUN python -m pip install --no-cache-dir /app/packages/core /app/packages/ai-gateway '/tmp/editorial-worker[test]'
COPY services/editorial-worker /app/services/editorial-worker
RUN python -m pip install --no-cache-dir --no-deps /app/services/editorial-worker
WORKDIR /app/services/editorial-worker
USER editorial
EXPOSE 8083
CMD ["python", "-m", "editorial_worker.main"]
