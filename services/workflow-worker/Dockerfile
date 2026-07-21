FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app

COPY packages/core /app/packages/core
COPY services/workflow-worker /app/services/workflow-worker
RUN python -m pip install --no-cache-dir /app/packages/core '/app/services/workflow-worker[test]'

WORKDIR /app/services/workflow-worker
USER app

EXPOSE 8081
CMD ["python", "-m", "workflow_worker.main"]
