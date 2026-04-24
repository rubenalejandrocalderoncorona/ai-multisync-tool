# ai-multisync-tool Makefile
# Targets for setting up the Docusaurus site and GitHub Actions workflows.

.PHONY: help install start build deploy add-central-workflow add-source-workflow setup

CENTRAL_REPO ?= your-org/your-central-docs-repo
TARGET_BRANCH ?= staging

help:
	@echo ""
	@echo "ai-multisync-tool — available targets"
	@echo ""
	@echo "  make install                  Install Node.js dependencies"
	@echo "  make start                    Run Docusaurus dev server (http://localhost:3000)"
	@echo "  make build                    Build static site to ./build"
	@echo "  make deploy                   Deploy to GitHub Pages (uses GITHUB_TOKEN or PAT)"
	@echo ""
	@echo "  make add-central-workflow     Copy sync-docs-central.yml to .github/workflows/"
	@echo "  make add-source-workflow      Copy sync-docs-source.yml for use in a source repo"
	@echo ""
	@echo "  make setup                    Full first-time setup: install deps + copy workflow"
	@echo ""
	@echo "Variables:"
	@echo "  CENTRAL_REPO   — full name of this central docs repo (org/repo)"
	@echo "  TARGET_BRANCH  — branch to sync docs into (default: staging)"
	@echo ""

install:
	npm install

start: install
	npm run start

build: install
	npm run build

deploy: install
	npm run deploy

# Copy the central workflow into the current repo (run from your central docs repo root)
add-central-workflow:
	@if [ ! -d ".github/workflows" ]; then mkdir -p .github/workflows; fi
	@cp $(CURDIR)/.github/workflows/sync-docs-central.yml .github/workflows/sync-docs-central.yml
	@echo "Copied sync-docs-central.yml to .github/workflows/"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Commit and push to the main branch of your central docs repo."
	@echo "  2. Add secrets: DOCS_SYNC_PAT and AI_API_KEY in repository settings."
	@echo "  3. (Optional) Set variables: AI_API_HOST, AI_API_PATH, AI_MODEL, GITHUB_HOST."

# Print the source workflow with instructions (run from your source service repo)
add-source-workflow:
	@if [ ! -d ".github/workflows" ]; then mkdir -p .github/workflows; fi
	@cp $(CURDIR)/.github/workflows/sync-docs-source.yml .github/workflows/sync-docs.yml
	@echo "Copied sync-docs-source.yml to .github/workflows/sync-docs.yml"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Open .github/workflows/sync-docs.yml and set CENTRAL_REPO and TARGET_BRANCH."
	@echo "  2. Add secret DOCS_SYNC_PAT to this source repo."
	@echo "  3. Commit and push — the workflow triggers on pushes to docs/** paths."

# First-time setup for the central docs repo
setup: install add-central-workflow
	@echo ""
	@echo "Setup complete. Edit docusaurus.config.js to set your site URL, then run:"
	@echo "  make start    — to preview locally"
	@echo "  make deploy   — to publish to GitHub Pages"
