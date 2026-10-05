# ai-multisync-tool Makefile

.PHONY: help install test test-integration demo stack-up stack-down stack-check k8s-render image add-central-workflow add-source-workflow

PY      = .venv/bin/python
COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml

help:
	@echo ""
	@echo "ai-multisync-tool"
	@echo ""
	@echo "  make install           Create .venv and install the dependencies (Python 3.11+)"
	@echo "  make test              Run the unit and CLI tests (no network, no services needed)"
	@echo "  make test-integration  Run the tests against real Qdrant + Postgres (QDRANT_URL, FACTSTORE_DATABASE_URL)"
	@echo "  make demo              Offline rehearsal of the five scenarios (scripted LLM, in-memory stores)"
	@echo "  make image             Build the pipeline image (linux/amd64) as multisync-pipeline:local"
	@echo ""
	@echo "  make stack-up          Start Qdrant + Postgres FactStore (needs .env, see .env.example)"
	@echo "  make stack-check       Create collections/schema and verify both services are reachable"
	@echo "  make stack-down        Stop the stack (data volumes are kept)"
	@echo "  make k8s-render        Render infra/k8s with kustomize (cluster migration check)"
	@echo ""
	@echo "  make add-central-workflow   Copy the manual-fallback and approval workflows into this repo's .github/workflows"
	@echo "  make add-source-workflow    Copy the source workflow (run from a service repo)"
	@echo ""

install:
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

test:
	$(PY) -m pytest tests --ignore=tests/integration

test-integration:
	$(PY) -m pytest tests/integration

demo:
	$(PY) -m multisync.cli.demo --offline

image:
	docker buildx build --platform linux/amd64 -t multisync-pipeline:local --load .

stack-up:
	@test -f .env || (echo "Missing .env: cp .env.example .env and fill in secrets" && exit 1)
	$(COMPOSE) up -d

stack-check:
	@test -f .env || (echo "Missing .env" && exit 1)
	$(COMPOSE) --profile tools run --rm --build pipeline

stack-down:
	$(COMPOSE) down

k8s-render:
	kubectl kustomize infra/k8s

add-central-workflow:
	@mkdir -p .github/workflows
	@cp $(CURDIR)/.github/workflows/sync-docs-central.yml $(CURDIR)/.github/workflows/sync-docs-approved.yml .github/workflows/ 2>/dev/null || true
	@echo "Commit both workflows to the DEFAULT branch of the central docs repo."
	@echo "Secrets:   DOCS_SYNC_PAT, AI_API_KEY, DEEPSEEK_API_KEY, FACTSTORE_DATABASE_URL"
	@echo "Variables: QDRANT_URL (+ optional AI_API_BASE_URL, AI_EXPENSIVE_MODEL, AI_CHEAP_MODEL, AI_EMBED_MODEL, AI_EMBED_DIM)"

add-source-workflow:
	@mkdir -p .github/workflows
	@cp $(CURDIR)/.github/workflows/sync-docs-source.yml .github/workflows/sync-docs.yml
	@echo "Edit CENTRAL_REPO and TARGET_BRANCH in .github/workflows/sync-docs.yml, add secret DOCS_SYNC_PAT."
