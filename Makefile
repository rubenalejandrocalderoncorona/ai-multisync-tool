# ai-multisync-tool Makefile

.PHONY: help install start build test stack-up stack-down stack-check k8s-render add-central-workflow add-source-workflow setup

COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml

help:
	@echo ""
	@echo "ai-multisync-tool"
	@echo ""
	@echo "  make install        Install Node.js dependencies"
	@echo "  make start          Astro Starlight dev server (http://localhost:4321)"
	@echo "  make build          Build the static site to ./dist"
	@echo "  make test           Run pipeline unit tests (no network, no services needed)"
	@echo ""
	@echo "  make stack-up       Start Qdrant + Postgres FactStore (needs .env, see .env.example)"
	@echo "  make stack-check    Create collection/schema and verify both services are reachable"
	@echo "  make stack-down     Stop the stack (data volumes are kept)"
	@echo "  make k8s-render     Render infra/k8s with kustomize (cluster migration check)"
	@echo ""
	@echo "  make add-central-workflow   Copy central workflows into this repo's .github/workflows"
	@echo "  make add-source-workflow    Copy the source workflow (run from a service repo)"
	@echo ""

install:
	npm install

start: install
	npm run start

build: install
	npm run build

test:
	npm test

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
	@echo "Secrets:   DOCS_SYNC_PAT, AI_API_KEY, QDRANT_API_KEY, FACTSTORE_DATABASE_URL"
	@echo "Variables: QDRANT_URL (+ optional RUNNER_LABEL, AI_API_BASE_URL, AI_MODEL, AI_EMBED_MODEL, AI_EMBED_DIM)"

add-source-workflow:
	@mkdir -p .github/workflows
	@cp $(CURDIR)/.github/workflows/sync-docs-source.yml .github/workflows/sync-docs.yml
	@echo "Edit CENTRAL_REPO and TARGET_BRANCH in .github/workflows/sync-docs.yml, add secret DOCS_SYNC_PAT."

setup: install add-central-workflow
	@echo "Next: set your site URL in astro.config.mjs, register repos in config/repos.json, then 'make start'."
