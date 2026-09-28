# Every target also works without make: `uv run recall <target>` locally, or
# `docker compose exec app recall <target>` against the running stack.
.PHONY: up down migrate demo test lint typecheck eval bench teams-package setup-teams fmt

up:
	docker compose up -d --build
	@echo "Recall is starting on http://localhost:8000 (docs at /docs). Try: make demo"

down:
	docker compose down

migrate:
	docker compose exec app recall migrate

demo:
	docker compose exec app recall demo

bench:
	docker compose exec app recall bench

test:
	uv run pytest

lint:
	uv run ruff check src tests scripts alembic
	uv run ruff format --check src tests scripts alembic

typecheck:
	uv run mypy src scripts

fmt:
	uv run ruff format src tests scripts alembic
	uv run ruff check --fix src tests scripts alembic

eval:
	uv run recall eval

teams-package:
	uv run recall teams-package

setup-teams:
	uv run recall setup-teams
