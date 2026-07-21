.PHONY: help config build up up-research up-publishing up-observability down ps logs migrate test test-unit smoke security-audit

help:
	@echo "config     validate the resolved Compose model"
	@echo "build      build application images"
	@echo "up         start the core profile"
	@echo "up-research start core plus the research profile"
	@echo "up-publishing start core plus the disabled-by-default publisher"
	@echo "up-observability start core plus private metrics/logs and Grafana"
	@echo "down       stop the stack without deleting data"
	@echo "migrate    apply API database migrations"
	@echo "test       run Python and web tests"
	@echo "smoke      exercise the running API through Caddy"

config:
	docker compose config --quiet

build:
	docker compose build

up:
	docker compose up --build -d

up-research:
	docker compose --profile core --profile research up --build -d

up-publishing:
	docker compose --profile core --profile publishing up --build -d

up-observability:
	OTEL_ENABLED=true docker compose --profile core --profile observability up --build -d

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs --tail=200

migrate:
	docker compose run --rm api alembic upgrade head

test: test-unit
	docker build --target builder -f apps/web/Dockerfile .

test-unit:
	docker compose run --rm api pytest -q -p no:cacheprovider
	docker compose run --rm workflow-worker pytest -q -p no:cacheprovider
	docker compose --profile core --profile research run --rm research-worker pytest -q -p no:cacheprovider
	docker compose --profile core run --rm editorial-worker pytest -q -p no:cacheprovider
	docker compose --profile core --profile publishing run --rm publisher-worker pytest -q -p no:cacheprovider

smoke:
	./scripts/smoke-core.sh

security-audit:
	./scripts/security-audit.sh
