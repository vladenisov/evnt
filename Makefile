# evnt: convenience commands. Run `make` (or `make help`) for the list.
#
# The backend uses uv (backend/, uv.lock); the frontend uses Bun (frontend/,
# bun.lock). Targets cd into the right subproject, so everything runs
# from the repo root. They wrap the exact commands CI runs and CONTRIBUTING.md
# documents; nothing here changes how the tools are invoked.

ROOT := $(patsubst %/,%,$(dir $(realpath $(firstword $(MAKEFILE_LIST)))))
BACKEND := $(ROOT)/backend
FRONTEND := $(ROOT)/frontend

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

##@ Setup
.PHONY: install install-be install-fe install-hooks
install: install-be install-fe install-hooks ## Install backend + frontend deps, the Iglu schemas and git hooks

install-be: ## Install backend deps with every extra (CI parity) and the iglu-central submodule
	git -C $(ROOT) submodule update --init --depth 1
	cd $(BACKEND) && uv sync --all-extras

install-fe: ## Install frontend deps (bun install --frozen-lockfile)
	cd $(FRONTEND) && bun install --frozen-lockfile

install-hooks: ## Install the pre-commit hooks
	cd $(BACKEND) && uv run pre-commit install --config $(ROOT)/.pre-commit-config.yaml

##@ Dev
.PHONY: dev dev-be dev-fe db-init
dev: ## Run the full stack via docker compose (watch mode)
	docker compose -f $(ROOT)/compose.yml up --watch

dev-be: ## Run just the API with reload (needs ClickHouse on localhost:8123)
	cd $(BACKEND) && EVNT_CLICKHOUSE__CONNECTION__HOST=$${EVNT_CLICKHOUSE__CONNECTION__HOST:-localhost} \
		uv run uvicorn evnt.main:app --reload --port 8000

dev-fe: ## Run just the demo SPA dev server (Vite)
	cd $(FRONTEND) && bun run dev

db-init: ## Create the ClickHouse tables in the compose stack
	docker compose -f $(ROOT)/compose.yml run --rm app evnt db init

##@ Quality gates
.PHONY: check lint lint-be lint-fe format typecheck typecheck-be typecheck-fe test test-be test-fe build-fe
check: lint typecheck test build-fe ## Run local gates (lint + types + tests + production build)

lint: lint-be lint-fe ## Lint backend + frontend

lint-be: ## Lint backend (ruff check + format --check)
	cd $(BACKEND) && uv run ruff check && uv run ruff format --check

lint-fe: ## Lint frontend (oxlint)
	cd $(FRONTEND) && bun run lint

format: ## Auto-format backend (ruff format + safe fixes)
	cd $(BACKEND) && uv run ruff check --fix && uv run ruff format

typecheck: typecheck-be typecheck-fe ## Type-check backend + frontend

typecheck-be: ## Type-check backend (mypy --strict)
	cd $(BACKEND) && uv run mypy

typecheck-fe: ## Type-check frontend
	cd $(FRONTEND) && bun run typecheck

test: test-be test-fe ## Run backend + frontend tests

test-be: ## Run backend tests. Extra args: make test-be ARGS="-k proxy -v"
	cd $(BACKEND) && uv run pytest $(ARGS)

test-fe: ## Run frontend tests. Extra args: make test-fe ARGS=clickhouse
	cd $(FRONTEND) && bun run test $(ARGS)

build-fe: ## Production build of the demo SPA
	cd $(FRONTEND) && bun run build

##@ Docker
.PHONY: image
image: ## Build the production image (tag: evnt:local)
	docker build -t evnt:local $(ROOT)
