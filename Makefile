.DEFAULT_GOAL := help
SHELL := /bin/bash

# The single source of truth for how to build, test, and run this repo.
# CI, the .claude/skills, and the README all call these targets, so there is
# one definition of each command rather than three that drift apart.

UV ?= uv
COMPOSE ?= docker compose
E2E_URL ?= http://localhost:$${APP_HOST_PORT:-8000}

.PHONY: help install lock fmt fmt-check lint typecheck test test-unit test-integration \
        cov check up down reset logs logs-worker ps migrate revision e2e sdl clean \
        ingest backfill report runs ui logs-ui observe janitor agent-runs logs-agents

help: ## List the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Sync the virtualenv from uv.lock, including dev tools
	$(UV) sync --all-groups

lock: ## Re-resolve dependencies into uv.lock
	$(UV) lock

fmt: ## Format and auto-fix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fmt-check: ## Check formatting without writing (CI)
	$(UV) run ruff format --check .

lint: ## Lint
	$(UV) run ruff check .

typecheck: ## Type-check the package under mypy strict
	$(UV) run mypy

test: ## Run every tier that can run here
	$(UV) run pytest

test-unit: ## Offline tests only; needs no Docker and no network
	$(UV) run pytest tests/unit

test-integration: ## Tests against a throwaway Postgres
	$(UV) run pytest tests/integration -m integration

cov: ## Run the suite with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing

check: fmt-check lint typecheck test ## Everything CI runs

up: ## Build and start the stack, waiting until it is healthy; prints the API log if it does not get there
	@$(COMPOSE) up -d --build --wait || { \
		echo; echo "--- the stack did not become healthy; last lines of the API container: ---"; \
		$(COMPOSE) logs --no-color --tail=40 candlegraph; \
		echo "--- if the log names a migration or volume problem, 'make reset' rebuilds the volume ---"; \
		exit 1; }

down: ## Stop the stack and delete its volume
	$(COMPOSE) down -v --remove-orphans

reset: ## Rebuild from nothing: stop, delete the volume and this project's image, start again
	$(COMPOSE) down -v --remove-orphans --rmi local
	$(MAKE) up

logs: ## Follow the application log
	$(COMPOSE) logs -f candlegraph

logs-worker: ## Follow the ingest worker log
	$(COMPOSE) logs -f worker

logs-ui: ## Follow the UI log
	$(COMPOSE) logs -f ui

logs-agents: ## Follow the agents log (observer reports land here)
	$(COMPOSE) logs -f agents

ui: ## Run the Streamlit UI locally against the stack's database
	$(UV) run --extra ui python -m candlegraph ui

ps: ## Show the stack's status
	$(COMPOSE) ps

migrate: ## Apply migrations to the configured database
	$(UV) run alembic upgrade head

revision: ## Create a migration: make revision m="add thing"
	@test -n "$(m)" || { echo 'usage: make revision m="description"'; exit 1; }
	$(UV) run alembic revision -m "$(m)"

e2e: ## Run the end-to-end tests against a running stack
	CANDLEGRAPH_E2E_URL=$(E2E_URL) $(UV) run pytest tests/e2e -m e2e

ingest: ## Run one ingest pass inside the stack
	$(COMPOSE) exec worker python -m candlegraph ingest

backfill: ## Backfill a longer window in the stack: make backfill days=90
	$(COMPOSE) exec worker python -m candlegraph backfill --days $${days:-90}

report: ## Print the data-quality report from the stack
	$(COMPOSE) exec worker python -m candlegraph report

runs: ## Show recent ingest runs from the ledger
	$(COMPOSE) exec worker python -m candlegraph runs

observe: ## Run the run-observer agent once inside the stack and print its report
	$(COMPOSE) exec agents python -m candlegraph observe $(ARGS)

janitor: ## Run the CI janitor locally: make janitor ARGS="--open-pr --strict"
	$(UV) run python -m candlegraph janitor $(ARGS)

agent-runs: ## Show recent agent runs from the agent ledger
	$(COMPOSE) exec agents python -m candlegraph agent-runs $(ARGS)

sdl: ## Regenerate the committed GraphQL schema (tests fail if it is stale)
	$(UV) run python -c "from candlegraph.api.schema import chart_schema; print(chart_schema)" > schema.graphql

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
