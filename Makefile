.PHONY: setup test lint fmt check campaign notebook serve clean login-cf login-wrangler account build deploy preview beads-sync

setup:
	uv sync --locked
	npm ci --prefix worker

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

login-cf:
	npm --prefix worker run login:cf

login-wrangler:
	npm --prefix worker run login:wrangler

account:
	npm --prefix worker run account

build:
	npm --prefix worker run build

deploy:
	npm --prefix worker run deploy

preview:
	npm --prefix worker run preview -- --run-strategy

beads-sync:
	bd export -o .beads/issues.jsonl --sandbox
	GIT_AUTHOR_NAME="Beads Sync" GIT_AUTHOR_EMAIL="beads-sync@users.noreply.github.com" GIT_COMMITTER_NAME="Beads Sync" GIT_COMMITTER_EMAIL="beads-sync@users.noreply.github.com" bd dolt push --remote origin --no-adopt --sandbox

clean:
	rm -rf .venv .pytest_cache .ruff_cache htmlcov .coverage
