FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app

COPY packages/core /app/packages/core
COPY services/api/pyproject.toml /tmp/api/pyproject.toml
COPY services/api/src/youtuber_api/__init__.py /tmp/api/src/youtuber_api/__init__.py
RUN python -m pip install --no-cache-dir /app/packages/core '/tmp/api[test]'

COPY services/api /app/services/api
COPY config /app/config
RUN python -m pip install --no-cache-dir --no-deps /app/services/api

WORKDIR /app/services/api
USER app

EXPOSE 8000
CMD ["/bin/sh", "-c", "alembic upgrade head && exec uvicorn youtuber_api.main:app --host 0.0.0.0 --port 8000"]
