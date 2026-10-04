# ai-multisync-tool

Sync documentation from many service repositories into one **Astro Starlight** site, with an AI decision pipeline that spends tokens only when a change deserves it, verifies every draft against its source, and never publishes something it could not ground.

```
Service repo ──push docs──► sync-docs-source.yml ──repository_dispatch──► sync-docs-central.yml
                                                                               │
                                          ┌────────────────────────────────────┤ decision pipeline
                                          ▼                                    ▼
                                  Qdrant (retrieval)                  Postgres FactStore (claims + audit)
```

## The decision pipeline

Cheap checks run first; each layer stops the run before the next, more expensive one.

| # | Layer | Cost | Stops the run when |
|---|---|---|---|
| 1 | **Prefilter** (`pipeline/prefilter.js`) | none | diff is below `MIN_DIFF_LINES` (`trivial_diff`), or nothing structural or fact-bearing changed (`no_structural_change`) |
| 2 | **Cross-repo gate** (`pipeline/registry.js`) | FactStore lookup | a registered contract point is not documented by the repos that must also ship it (`cross_repo_incomplete`) |
| 3 | **Similarity** (`pipeline/pipeline.js`) | embeddings | shape is unchanged and every chunk is ≥ `SIMILARITY_HIGH` similar to approved text → only the commit hash is re-keyed (anti-staleness) |
| 4 | **Generate + judge loop** (`writer.js`, `critic.js`) | LLM | passes, or falls back (below) |
| 5 | **Publish by trust level** | none | `auto` → committed and indexed; `review` → pull request for a technical writer |

A *shape* change (heading, code block, table row, list item, function or array element added or removed) overrides high similarity, because adding one item barely moves an embedding.

**Generation** uses RAG plus GAR: the model first writes a hypothetical paragraph describing the change, and its embedding retrieves matching approved chunks as terminology context. That hypothetical text is never indexed.

**The critic** (role) uses an LLM-as-judge (technique) to extract claims from the draft and facts from the source, then code computes:

- `precision` = supported claims / claims → below `PRECISION_MIN` is a hallucination (`hallucinated_claim`)
- `recall` = covered source facts / source facts → below `RECALL_MIN` is a missing claim (`missing_claim`)
- `style` and `quality` scores against the repo's style guide and glossary (`style_mismatch`, `judge_low_confidence`)

A grounded-but-awkward draft gets one **polish-only** pass (forbidden from touching facts) and is re-scored. Otherwise the findings are fed back into the next attempt. After `MAX_ITERATIONS`, **one automatic retry** runs with `TOP_K_WIDENED` context before escalating.

### Two context stages (code mode)

The vector database holds the whole repository before anything is analysed, in two collections: **code context** (every in-scope source file, chunked) and **semantic context** (approved pages, the repo's own docs, page briefs). A `sync_context` step runs first on every sync: the first run, or any run where the index is not exactly at the previous commit, loads the whole repo; otherwise only changed files are re-embedded.

Then two separate LLM stages read that context:

1. **`code_context` (stage 1):** sees the changed code, the repo map, and related code retrieved from the whole repo. It produces a fact sheet; every fact must cite its evidence.
2. **`semantic_context` (stage 2):** sees the fact sheet, the page brief, the style rubric, the existing page and related docs. It produces a plan: sections, which facts each must cover, terminology, and the gaps the code cannot support.

The writer follows the plan; the judge checks every claim against the code (including the related code) and checks coverage against the plan. A widened retry re-runs both stages with a bigger budget. Load a repo ahead of time with `npm run bootstrap -- --repo owner/name --dir <checkout>` or the **Bootstrap Context** workflow.

### Two modes

| `mode` in `config/repos.json` | Source of truth | Typical repo |
|---|---|---|
| `docs` (default) | changed files under `docs/`, `documentation/`, or the root `README.md` | repos that already keep docs |
| `code` | the source code itself; the pipeline drafts and updates declared pages | repos with little or no docs |
| `both` | both of the above | |

In `code` mode each repo declares the pages it wants and which files document them. A commit only regenerates the pages whose files changed; tests, lockfiles, vendored and generated files are never read, and lines that look like secrets are scrubbed before anything reaches a model or a log.

```json
"rubenalejandrocalderoncorona/rurag": {
  "mode": "code",
  "style": "README / project overview",
  "pages": [
    { "path": "overview.md", "kind": "Portfolio case study", "scope": ["**"] },
    { "path": "api.md", "kind": "API documentation", "scope": ["src/routes/**", "openapi.yaml"] }
  ]
}
```

In code mode the similarity stage embeds the GAR paragraph (what the docs would say about this change) and compares it with what the page already says, because code and prose are not comparable. The judge then checks every claim against the code (`prompts/judge-code.md`). A first run, or `full: true` on a manual dispatch, documents everything in scope.

### Prompts and styles

All LLM instructions are files in `prompts/` (see `prompts/README.md`); each run logs the prompt version as `name@hash`. Writer personas and judge rubrics are a key-value library, `config/doc-styles.json`:

```json
"API documentation": {
  "prompt": "You are a backend developer writing API documentation for engineers who will integrate with this service. ...",
  "rubric": ["Each endpoint lists method/path, parameters with types and errors", "..."]
}
```

The key is chosen per repo (`style`) or per page (`kind`). `prompt` becomes the writer persona; `rubric` is what the judge scores `style` against. Bundled keys: API documentation, README / project overview, Architecture overview, How-to guide, Runbook / SOP, Configuration reference, Troubleshooting, Tutorial, Release notes, Portfolio case study, Data and schema reference. Add keys freely.

### LangGraph stages and logging

The cascade is a LangGraph `StateGraph` (`pipeline/pipeline.js`). Nodes: `prefilter` → `cross_repo` → `similarity` → `gar` → `write_draft` → `judge` → (`polish_draft` | `widen`) → `publish` or `fallback`.

Every node execution is logged with its status, duration and decision data (for example `minChunkSimilarity`, `precision`, `topK`):

- to the runner log, one line per node;
- to the FactStore `node_logs` table (queryable per `run_id`);
- in the returned decision as `trail`, and in `pipeline-results.json` as `stages`;
- optionally to LangSmith by setting `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` (LangGraph's native tracing; runs are named `docs-sync-decision`).

```sql
SELECT node, status, ms, note FROM node_logs WHERE run_id = '<run>' ORDER BY id;
```

### Fallback (never auto-publishes)

A failed run records `reviewer_action = auto_rejected` with a `root_cause_tag` (`iteration_cap_exceeded`, `cross_repo_incomplete`, `pipeline_error`; the per-attempt check that failed, e.g. `hallucinated_claim`, is in the attempt table), opens a ticket in **cAImanDesk** (default; GitHub Issues and Jira are alternatives) with the draft, attempt table and failed checks, posts to Slack if configured, and uploads the rejected draft as a workflow artifact. No page is written. `FactStore.rootCauseBacklog(repo)` shows which causes repeat per repo.

### The index only holds approved text

| Path | When the vector store is written |
|---|---|
| `trust: auto` | immediately after publish |
| `trust: review` | **only when the PR is merged** (`sync-docs-approved.yml`), keyed by commit hash |
| draft, rejected, GAR text | never |

### Ticketing (cAImanDesk)

`https://tickets.caimanlabs.com.mx` is a Vikunja v2 deployment, so tickets are created through its REST API: `PUT /api/v1/projects/{id}/tasks` with a Bearer API token. Set `CAIMANDESK_API_TOKEN` (an API token allowed to create tasks) and `CAIMANDESK_PROJECT_ID`. A repeat failure for the same repo, file and root cause adds a comment to the open task instead of creating a duplicate. Ticket creation never blocks or fails a run.

## Demo

```bash
npm run demo:offline     # rehearsal: scripted LLM, in-memory stores, no keys needed
npm run demo             # live: real LLM + embeddings, Qdrant, Postgres, cAImanDesk
```

Five scenarios, each printing its stage trail: first publish, near-duplicate (cosine short-circuit, no LLM), structural change (overrides similarity), cross-repo block (fallback + ticket, no LLM), and judge fallback (precision forced above 1.0, so the loop widens and escalates + ticket). The live demo needs the variables in `.env.example`; `SIMILARITY_HIGH` defaults to 0.85 there and must be calibrated against real embeddings.

## CI/CD

`.github/workflows/ci.yml` runs on every PR and push: unit and CLI tests, **integration tests against real Qdrant and Postgres service containers**, workflow lint (`actionlint`), compose and kustomize validation, a secret scan, an image build and smoke test; pushes to `main` publish `ghcr.io/<owner>/ai-multysinc-pipeline` with build provenance. `deploy-cluster.yml` (manual, behind an Environment approval) applies `infra/k8s` to the VPS cluster. Run the integration tests yourself with `QDRANT_URL=... FACTSTORE_DATABASE_URL=... npm run test:integration`; they use unique names and clean up, so they are safe to point at a shared instance.

## Repository layout

| Path | Purpose |
|---|---|
| `pipeline/` | the decision pipeline (config, llm, structure, prefilter, vectorstore, factstore, registry, writer, critic, fallback) |
| `scripts/` | CLI entry points: `run_pipeline.js`, `bootstrap_context.js`, `index_approved.js`, `healthcheck.js`, `doctor.js`, `generate-summary.js`, `demo.js` |
| `config/repos.json` | per-repo trust level, docs folder, style guide, glossary |
| `config/feature-registry.json` | contract-point symbol → repos that must also ship |
| `infra/` | docker-compose stack, k8s manifests for the `multirepo` namespace (migrate Job, in-cluster runner) and a standalone variant |
| `.github/workflows/` | source, central, and post-approval workflows |
| `test/` | unit tests with a scripted LLM (no network) |

## What you still need to provide

See [docs/SETUP-REQUIRED.md](docs/SETUP-REQUIRED.md): infrastructure diagram with what is missing, every credential and where to get it, and the order of operations. `node scripts/doctor.js` checks them.

## Quick start

### 1. Run the tests (no services needed)

```bash
make install && make test
```

### 2. Stand up the vector DB and FactStore on the VPS

```bash
cp .env.example .env        # set QDRANT_API_KEY and FACTSTORE_PASSWORD
make stack-up
make stack-check            # creates the Qdrant collection and FactStore schema
```

Ports bind to `127.0.0.1`. Do **not** publish Postgres. Pick one way for CI to reach the stack:

1. **Self-hosted GitHub runner on the VPS** (recommended): set repo variable `RUNNER_LABEL` to its label; `QDRANT_URL=http://localhost:6333`.
2. **WireGuard/Tailscale** between the VPS and your runner.
3. **`--profile edge`** for an HTTPS + API-key front on Qdrant only (Postgres still needs option 1 or 2).

`AI_EMBED_DIM` must match the embedding model (1536 for `text-embedding-3-small`). Changing the model means a new collection.

### 3. Configure the central docs repo

Commit `sync-docs-central.yml` and `sync-docs-approved.yml` to its **default branch**, then set:

| Kind | Name |
|---|---|
| secret | `DOCS_SYNC_PAT`, `AI_API_KEY`, `QDRANT_API_KEY`, `FACTSTORE_DATABASE_URL` |
| variable | `QDRANT_URL` |
| optional | `RUNNER_LABEL`, `AI_API_BASE_URL`, `AI_MODEL`, `AI_FAST_MODEL`, `AI_EMBED_MODEL`, `AI_EMBED_DIM`, `TICKET_PROVIDER`, `SLACK_WEBHOOK_URL`, `JIRA_*` |

Any OpenAI-compatible server works (OpenAI, vLLM, Ollama via `AI_API_BASE_URL=http://host:11434`).

### 4. Register each service repo

Add it to `config/repos.json` (start with `trust: review`), copy `sync-docs-source.yml` into the service repo, set `CENTRAL_REPO`, `TARGET_BRANCH`, and add `DOCS_SYNC_PAT`. The source workflow needs no AI key.

## Moving to Kubernetes

`infra/k8s/` mirrors the compose stack: StatefulSets with PVCs, probes taken from the compose healthchecks, a `multisync-migrate` Job, and a Secret template. Everything is configured by environment variables, so the same `Dockerfile.pipeline` image runs as a Job or CronJob. Check it with `make k8s-render`.

## Reliability

The LLM client retries rate limits (429), server errors and network failures, honoring the server's own "try again in Ns" hint (`AI_MAX_RETRIES`, default 5). A stage that still fails raises a `pipeline_error` fallback; nothing is published. Large prompts on a low-tier OpenAI key can hit the tokens-per-minute limit: the retries absorb it, at the cost of a slower run.

## Tuning

Thresholds are environment variables (`MIN_DIFF_LINES`, `SIMILARITY_HIGH`, `PRECISION_MIN`, `RECALL_MIN`, `STYLE_MIN`, `JUDGE_MIN`, `MAX_ITERATIONS`, `TOP_K`, `TOP_K_WIDENED`); defaults are in `.env.example`. Start strict on precision, and loosen recall or style only after reading the fallback backlog.

## Security notes

- Dispatch fields are read through `env`, never interpolated into shell, and validated before use.
- The central workflow refuses to write to `main` or `master`.
- Pipeline code is always taken from the default branch, not from the target branch.
- Infrastructure errors fail safe (`pipeline_error` fallback), never publish.

## Known limits

- Structural analysis is a lexical signature, not a parser: it is language-agnostic but approximate.
- Code mode reads a size-capped snapshot (60k characters, 12k per file) of each page's scoped files; very large repos need narrow `scope` globs per page.
- The Qdrant REST adapter and Postgres FactStore have been tested only through in-memory twins and request-shape review, not against live services in this repo's CI.
