# ASTRA — local qualification targets.
#
# A green target is evidence that covered software contracts hold. It is never
# permission to route orders and never a profitability claim.

SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
DSN ?= postgresql://astra:astra@127.0.0.1:5433/astra
# The fleet-deployment tests need their own database; see test-pg.
FLEET_DSN ?= postgresql://astra:astra@127.0.0.1:5433/astra_fleet

.DEFAULT_GOAL := help
.PHONY: help venv install test test-pg test-pg-fresh migrate verify-migrations status lint lint-all typecheck security audit db-up db-down db-reset compose-test clean

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python3 -m venv $(VENV)

install: venv ## Install the package with development extras
	$(PIP) install --disable-pip-version-check -e '.[dev]'
	$(PIP) check

test: ## Run the suite without PostgreSQL (27 integration modules skip)
	$(PY) -m pytest -q

# The fleet-deployment tests get their own database. CI never runs them alongside the
# rest of the suite - canonical-deployment-regression gives them a fresh service
# container - so sharing one database lets earlier tests leave state that makes the v105
# replay-fencing assertion fail while every test still passes in isolation.
test-pg: ## Run the full suite against PostgreSQL (expects freshly reset databases)
	ASTRA_TEST_POSTGRES_DSN=$(DSN) ASTRA_TEST_FLEET_DEPLOYMENT_DSN=$(FLEET_DSN) \
	  $(PY) -m pytest -q

test-pg-fresh: db-reset test-pg ## Reset the database, migrate, then run the full suite

migrate: ## Apply the platform migration lineage twice to both databases
	ASTRA_POSTGRES_DSN=$(DSN) $(PY) tools/bootstrap_db.py
	ASTRA_POSTGRES_DSN=$(FLEET_DSN) $(PY) tools/bootstrap_db.py

verify-migrations: ## Check migrations against their packaged copies, no database needed
	$(PY) tools/bootstrap_db.py --verify-only

status: ## Compile and validate the generated status surface
	$(PY) tools/system_status.py --check
	$(PY) tools/system_status.py --show

# CI lints a hand-maintained allowlist of paths rather than the whole tree, so parts of
# app/runtime, tools/ and tests/ carry findings that have never been gated. `lint` covers
# the set CI keeps clean, so it is safe to gate on; `lint-all` surveys everything and is
# expected to report that legacy backlog.
lint: ## Run ruff over the paths CI keeps clean
	$(VENV)/bin/ruff check app/domain app/marketdata app/strategy app/risk app/portfolio \
	  app/oms app/application app/execution app/observability tools/bootstrap_db.py

lint-all: ## Run ruff over the whole tree, including the ungated legacy backlog
	-$(VENV)/bin/ruff check app tools tests

typecheck: ## Run mypy over the application
	$(VENV)/bin/mypy app

security: ## Run bandit over the application
	$(VENV)/bin/bandit -q -r app

audit: ## Audit locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit -r requirements.lock

db-up: ## Start PostgreSQL and wait until it accepts connections
	docker compose up -d postgres
	@until docker compose exec -T postgres pg_isready -U astra -d astra >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on port $${POSTGRES_PORT:-5433}"

db-down: ## Stop PostgreSQL, keeping its volume
	docker compose down

# The execution-accounting tables are append-only behind BEFORE TRUNCATE guards, so a
# durable run cannot be undone by truncation. Planning is gated globally on
# unresolved_count() == 0, which means facts left by an earlier run make the next run
# raise EXECUTION_ACCOUNTING_NOT_CONVERGED. CI sidesteps this with a fresh service
# container per job; locally the schema has to be recreated instead. This target is kept
# separate from test-pg, and refuses a non-local DSN, so that no test target can drop a
# schema that holds real trading state.
db-reset: ## Drop and recreate the schema (local DSN only; destroys all durable state)
	@case "$(DSN)" in \
	  *@127.0.0.1:*|*@localhost:*|*@postgres:*) ;; \
	  *) echo "REFUSING: DSN '$(DSN)' is not local. Reset it by hand if that is really intended."; exit 1 ;; \
	esac
	@docker compose exec -T postgres psql -U astra -d postgres -q \
	  -c "CREATE DATABASE astra_fleet OWNER astra;" 2>/dev/null || true
	ASTRA_POSTGRES_DSN=$(DSN) $(PY) tools/bootstrap_db.py --reset
	ASTRA_POSTGRES_DSN=$(FLEET_DSN) $(PY) tools/bootstrap_db.py --reset

compose-test: ## Bootstrap the schema and run the full suite in containers
	docker compose run --rm migrate
	docker compose run --rm tests

clean: ## Remove build and cache artifacts, leaving the virtualenv in place
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
