# Local development. `make help` lists the targets; README.md explains them.
#
# Everything here runs in the local profile (CLOUDOPS_ENV=local) against the
# docker-compose stack. Nothing here can reach Azure: the config layer refuses
# local mode inside a cluster, and the seed refuses any profile but local.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
.PHONY: help env up migrate seed test check lint down reset psql logs ps api token

ENV_FILE := .env.local
COMPOSE := docker compose --env-file $(ENV_FILE)
SECRET := uv run --quiet python -m cloudops_config secret

export CLOUDOPS_ENV := local
export CLOUDOPS_LOCAL_SECRETS_FILE := $(CURDIR)/$(ENV_FILE)

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

$(ENV_FILE):
	uv run python -m cloudops_devdata.envfile $(ENV_FILE)

env: ## create .env.local, or add keys later steps need (never changes a value)
	uv run python -m cloudops_devdata.envfile $(ENV_FILE)

up: env ## start the stack, wait until healthy, migrate, create blob containers
	$(COMPOSE) up --detach --build --wait
	$(MAKE) --no-print-directory migrate
	uv run python -m cloudops_devdata.provision
	@echo
	@echo "  postgres    127.0.0.1:5432  (db cloudops; make psql)"
	@echo "  redis       127.0.0.1:6379"
	@echo "  temporal    127.0.0.1:7233  UI http://localhost:8233"
	@echo "  azurite     http://127.0.0.1:10000/devstoreaccount1"
	@echo "  clickhouse  http://127.0.0.1:8123"
	@echo
	@echo "  next: make seed"

# The DSN reaches alembic as a file, as in the pipeline, through process
# substitution: it is never written to disk.
migrate: $(ENV_FILE) ## apply database migrations as cloudops_migrator
	env CLOUDOPS_MIGRATION_DSN_FILE=<($(SECRET) postgres-migration-dsn) uv run alembic upgrade head

seed: $(ENV_FILE) ## load the synthetic dataset, then check isolation (refuses if already seeded)
	uv run python -m cloudops_devdata.seed

api: env ## run the API on http://127.0.0.1:8000 (dev token issuer, OpenAPI at /openapi.json)
	uv run python -m cloudops_api

token: ## mint a dev token for a seeded principal: make token OID=<oid>
	@test -n "$(OID)" || { echo "usage: make token OID=<oid> (make seed prints them)"; exit 2; }
	@uv run --quiet python -m cloudops_devdata.tokens --oid $(OID)

# Tests provision their own throwaway databases on the local server, so they
# never touch seeded data. pg_partman is required, as in CI.
test: $(ENV_FILE) ## run every test, including the isolation suite, against the local Postgres
	CLOUDOPS_TEST_ADMIN_DSN="$$($(SECRET) postgres-admin-dsn)" CLOUDOPS_REQUIRE_PARTMAN=1 uv run pytest

lint: ## ruff, mypy --strict and the data-access gate
	uv run ruff check .
	uv run mypy --strict .
	uv run python -I tools/ci/check_data_access.py .

check: lint test ## what CI runs

down: ## stop the stack; data is kept in volumes
	$(COMPOSE) down

reset: $(ENV_FILE) ## wipe every local volume, then up and seed again
	$(COMPOSE) down --volumes
	$(MAKE) --no-print-directory up seed

psql: ## psql as the application login: RLS applies, so no scope means no rows
	$(COMPOSE) exec postgres psql -U cloudops_app_local -d cloudops

logs: ## follow container logs
	$(COMPOSE) logs --follow --tail=100

ps: ## container status
	$(COMPOSE) ps
