.PHONY: setup test lint fmt check campaign notebook serve clean

setup:
	uv sync --locked

test:
	uv run --locked pytest

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

check: lint test

campaign:
	uv run --locked wsorb campaign run --replicates 24

notebook:
	uv run --locked wsorb notebook index

serve:
	devenv up

clean:
	rm -rf .venv .pytest_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
