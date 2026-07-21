FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --gid 10002 research && useradd --uid 10002 --gid research --create-home research
COPY packages/core /app/packages/core
COPY services/research-worker /app/services/research-worker
RUN python -m pip install --no-cache-dir /app/packages/core '/app/services/research-worker[test]'
WORKDIR /app/services/research-worker
USER research
EXPOSE 8082
CMD ["python", "-m", "research_worker.main"]
