# Developer entry points. The virtualenv lives in ./env (uv-managed).
export UV_PROJECT_ENVIRONMENT := env
COMPOSE ?= docker compose

.PHONY: install requirements lint format typecheck test check up up-two-host down validate-stack

install:            ## create/sync ./env from uv.lock (incl. dev tools)
	uv sync --frozen

requirements:       ## re-export pinned requirements*.txt from uv.lock
	uv export -q --format requirements-txt --no-hashes --no-dev --no-emit-project --no-editable --no-header -o requirements.txt
	uv export -q --format requirements-txt --no-hashes --no-emit-project --no-editable --no-header -o requirements-dev.txt

lint:
	env/bin/ruff check .
	env/bin/ruff format --check .

format:
	env/bin/ruff check --fix .
	env/bin/ruff format .

typecheck:
	env/bin/mypy

test:
	env/bin/pytest

check: lint typecheck test

up:                 ## single-host dev stack
	COMPOSE_PROFILES=single $(COMPOSE) up -d --build

up-two-host:        ## two simulated hosts sharing one backend set
	COMPOSE_PROFILES=two-host $(COMPOSE) up -d --build

down:
	COMPOSE_PROFILES=single,two-host $(COMPOSE) down

validate-stack:     ## full P0 compose validation (both profiles)
	scripts/validate-stack.sh
