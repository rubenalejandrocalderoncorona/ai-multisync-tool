# ai-multisync-tool

Automatically sync documentation from any number of service repositories into a single Docusaurus site, with AI-driven quality checks, folder classification, template selection, and language polish.

---

## How it works

```
Service Repo A  ──push docs──►  sync-docs-source.yml  ──repository_dispatch──►  sync-docs-central.yml
Service Repo B  ──push docs──►  sync-docs-source.yml  ──repository_dispatch──►      │
                                                                                      ▼
                                                                          Central Docs Repo (this repo)
                                                                          Docusaurus site built and deployed
```

1. A developer pushes a change to `docs/**` in a service repo.
2. `sync-docs-source.yml` calls an AI to assess whether the change is worth syncing.
3. If relevant, it sends a `repository_dispatch` event to the central docs repo.
4. `sync-docs-central.yml` (running on `main`) receives the event, checks out the target branch, and runs the AI pipeline:
   - Classifies each file into the correct subfolder (how-to-guides, features, concepts, etc.)
   - Selects the best-matching documentation template
   - Applies the template and language polish using documentation standards
   - Injects frontmatter and writes files under `docs/services/<repo-name>/`
5. Changes are committed and pushed to the target branch.
6. Your existing deployment workflow builds and publishes the Docusaurus site.

---

## Prerequisites

Before setting up, ensure you have:

### 1. GitHub runner

The central workflow (`sync-docs-central.yml`) runs on `ubuntu-latest` by default. If your organization uses a self-hosted runner, change `runs-on: ubuntu-latest` to your runner label in both workflow files.

### 2. AI API key

The pipeline requires an OpenAI-compatible API. Any provider that exposes `/v1/chat/completions` works (OpenAI, Azure OpenAI, Anthropic via compatibility layer, local Ollama, etc.).

- **Default:** OpenAI `gpt-4o` at `api.openai.com`
- **Override:** Set the `AI_API_HOST`, `AI_API_PATH`, and `AI_MODEL` variables in your central repo

You will need a valid API key with access to your chosen model.

### 3. Personal Access Token (PAT)

Create a GitHub PAT (classic) or a fine-grained token with the following permissions on the **central docs repo**:

| Permission | Why |
|---|---|
| `contents: write` | Commit and push synced documentation |
| `workflows: write` | Trigger and update workflow runs (`repository_dispatch`) |

> **Fine-grained tokens:** Set the token on the central docs repo with Read/Write access to Contents and Workflows. Then add it as a secret named `DOCS_SYNC_PAT` in **every source service repo** that will dispatch to the central repo.

### 4. Node.js 18+

Required to run scripts locally or in CI. The workflow uses `actions/setup-node@v4` with Node 18.

---

## Quick start

### Step 1 — Set up the central docs repo

Clone or fork this repository, then configure it as your central docs site.

```bash
# Clone the tool
git clone https://github.com/your-org/ai-multisync-tool.git my-docs-site
cd my-docs-site

# Install dependencies
make install

# Preview locally
make start
```

Edit `docusaurus.config.js` to set your site URL, organization name, and project name.

### Step 2 — Add the central workflow

The file `.github/workflows/sync-docs-central.yml` must be committed to the **`main` branch** of your central docs repo. GitHub `repository_dispatch` events are only received by workflows on the default branch.

```bash
git add .github/workflows/sync-docs-central.yml
git commit -m "chore: add documentation sync central workflow"
git push origin main
```

### Step 3 — Add secrets and variables to the central repo

In your central docs repo settings (`Settings → Secrets and variables → Actions`):

**Secrets (required):**

| Name | Value |
|---|---|
| `DOCS_SYNC_PAT` | GitHub PAT with `contents:write` and `workflows:write` on this repo |
| `AI_API_KEY` | Your AI provider API key |

**Variables (optional — override defaults):**

| Name | Default | Description |
|---|---|---|
| `AI_API_HOST` | `api.openai.com` | AI API hostname |
| `AI_API_PATH` | `/v1/chat/completions` | AI API path |
| `AI_MODEL` | `gpt-4o` | Model name |
| `GITHUB_HOST` | `github.com` | Override for GitHub Enterprise |

### Step 4 — Add the source workflow to each service repo

Copy `.github/workflows/sync-docs-source.yml` into any service repository that should sync its docs.

```bash
# From within the service repo
cp /path/to/ai-multisync-tool/.github/workflows/sync-docs-source.yml \
   .github/workflows/sync-docs.yml
```

Open `.github/workflows/sync-docs.yml` and configure the top-level `env` block:

```yaml
env:
  CENTRAL_REPO: 'your-org/your-central-docs-repo'  # ← required
  TARGET_BRANCH: 'staging'    # ← branch in the central repo to write into
  INSTRUCTIONS_FILE: ''       # ← optional: path to custom instructions file
  SERVICE_NAME: ''            # ← optional: override docs folder name
  TARGET_PATH: ''             # ← optional: explicit destination path
  TEMPLATES_PATH: ''          # ← optional: service-specific templates dir
```

Add the `DOCS_SYNC_PAT` secret to the source service repo as well.

Commit and push — the workflow triggers on any push to `docs/**` or `documentation/**` paths.

### Step 5 — Configure the target branch protection (recommended)

Never sync directly to `main`. The central workflow refuses to write to `main` or `master` by design. Use a staging branch:

1. Create a `staging` branch (or any branch name — set it as `TARGET_BRANCH` in step 4).
2. Open pull requests from `staging` → `main` after reviewing synced content.
3. Your existing deploy workflow builds the Docusaurus site on merge to `main`.

---

## Customizing documentation standards

The file `.github/instructions/DocumentationInstructions.instructions.md` controls all AI behavior:

- **Formatting rules** — heading structure, code block style, callout format
- **Handling missing content** — whether to delete empty template sections or fill them with placeholders
- **Folder structure** — the `## Folder Structure` section tells the AI which subfolder to place each file in

Edit this file to match your team's documentation standards. No code changes are required.

### Per-service instructions

To use different standards for a specific service, create a file like `.github/instructions/my-service.md` in the **central docs repo** and set `INSTRUCTIONS_FILE: '.github/instructions/my-service.md'` in that service's source workflow.

---

## Adding templates

Templates are stored in `docs/templates/`. Each template is a Markdown file. The AI reads all template files, picks the best match for each document, and restructures the content accordingly.

To add a template:
1. Create `docs/templates/my-template/my-template.md`
2. Write your template with section headings
3. The AI will automatically consider it for future syncs — no code changes needed

---

## Deploying to GitHub Pages

```bash
# Build the site
make build

# Deploy to GitHub Pages (uses GIT_USER and GITHUB_TOKEN env vars)
GIT_USER=your-github-username make deploy
```

Or trigger deployment through your existing CI workflow on merge to `main`.

---

## Repository structure

```
.
├── .github/
│   ├── instructions/
│   │   └── DocumentationInstructions.instructions.md  # AI documentation standards
│   └── workflows/
│       ├── sync-docs-central.yml   # Lives on main branch of the central docs repo
│       └── sync-docs-source.yml    # Copy this into each source service repo
├── docs/
│   ├── services/                   # Auto-synced docs land here (one dir per service)
│   └── templates/                  # Documentation templates for AI template selection
│       ├── default-template/
│       ├── sop-template/
│       └── technical-concept-template/
├── scripts/
│   ├── analyze_docs.js             # AI: decide which files to sync and classify folders
│   ├── sync_docs.js                # AI: template selection, polish, frontmatter injection
│   ├── update_sidebar.js           # Update sidebar.js after sync
│   └── generate-summary.js         # Generate GitHub Actions step summary
├── docusaurus.config.js            # Docusaurus site configuration
├── sidebars.js                     # Sidebar configuration (autogenerated by default)
├── package.json
└── Makefile                        # Helper targets for setup and deployment
```

---

## Environment variables reference

### Central workflow (`sync-docs-central.yml`)

All configured via GitHub repository secrets and variables.

| Variable | Source | Description |
|---|---|---|
| `DOCS_SYNC_PAT` | Secret | PAT for checkout and push |
| `AI_API_KEY` | Secret | AI provider API key |
| `AI_API_HOST` | Variable | AI API hostname (default: `api.openai.com`) |
| `AI_API_PATH` | Variable | AI API path (default: `/v1/chat/completions`) |
| `AI_MODEL` | Variable | Model name (default: `gpt-4o`) |
| `GITHUB_HOST` | Variable | GitHub hostname (default: `github.com`, override for GHE) |

### Source workflow (`sync-docs-source.yml`)

Configured in the `env` block at the top of the workflow file.

| Variable | Description |
|---|---|
| `CENTRAL_REPO` | Full name of the central docs repo (`org/repo`) |
| `TARGET_BRANCH` | Branch in the central repo to write synced docs into |
| `INSTRUCTIONS_FILE` | Path to custom instructions file in the central repo |
| `SERVICE_NAME` | Override the service folder name (defaults to repo name) |
| `TARGET_PATH` | Explicit destination path (overrides `SERVICE_NAME`) |
| `TEMPLATES_PATH` | Service-specific templates directory in the central repo |

---

## Troubleshooting

**Workflow not triggered after push**
- Confirm the source workflow listens to the correct branch and paths.
- Check that `DOCS_SYNC_PAT` has `workflows:write` permission.

**`repository_dispatch` not received by central workflow**
- `sync-docs-central.yml` must be on the **`main` branch** of the central repo.
- Verify the PAT has `contents:write` and `workflows:write` on the central repo.

**AI returns empty or malformed responses**
- Check `AI_API_KEY` is correctly set as a secret.
- Verify `AI_API_HOST` and `AI_MODEL` match your provider's values.
- For Azure OpenAI, set `AI_API_PATH` to `/openai/deployments/YOUR_DEPLOYMENT/chat/completions?api-version=2024-02-01`.

**Template boilerplate appearing in output**
- Check that `DocumentationInstructions.instructions.md` contains the "Handling Missing Content" section.
- Ensure the file is committed to the branch the workflow runs on.

**Branch safety error: `FATAL: Refusing to sync directly to main`**
- Set `TARGET_BRANCH` to a non-protected branch (e.g. `staging`).

---

## License

MIT
