FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --gid 10005 publisher && useradd --uid 10005 --gid publisher --create-home publisher
COPY packages/core /app/packages/core
COPY services/publisher-worker/pyproject.toml /tmp/publisher-worker/pyproject.toml
COPY services/publisher-worker/src/publisher_worker/__init__.py /tmp/publisher-worker/src/publisher_worker/__init__.py
RUN python -m pip install --no-cache-dir /app/packages/core '/tmp/publisher-worker[test]'
COPY services/publisher-worker /app/services/publisher-worker
RUN python -m pip install --no-cache-dir --no-deps /app/services/publisher-worker
WORKDIR /app/services/publisher-worker
USER publisher
EXPOSE 8084
CMD ["python", "-m", "publisher_worker.main"]
