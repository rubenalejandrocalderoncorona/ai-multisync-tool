# Seeing what the pipeline did

Three levels, from the one that always exists to the one you opt into.

## 1. The FactStore: central, durable, free (always on)

Every node of the LangGraph graph writes a row to `multisync.node_logs` when it finishes (stage, status, milliseconds, and a JSON note with
the scores, the model tier, the tokens and the dollar cost, the retrieved context). The decision of each page goes to `multisync.decisions`.
Because a node logs when it ends, a run that dies halfway is still visible. Pod logs vanish five minutes after a Job ends; these rows do not.

```bash
python -m multisync.cli.runs                          # recent runs: repo, stages, errors, cost, outcomes
python -m multisync.cli.runs --repo owner/name
python -m multisync.cli.runs <run_id>                 # every stage of one run, with its decision data
python -m multisync.cli.runs <run_id> --json
```
Run it where Postgres is reachable (an ssh tunnel to `postgres-0`, with `FACTSTORE_DATABASE_URL` pointing at it).

## 2. LangSmith: hosted LangGraph traces (opt-in)

LangGraph is instrumented for LangSmith. With tracing on, every run shows as a tree in a web UI: each node, each model call with its exact
prompt and answer, latency and tokens per call, and a search by repo, page or commit (runs carry the metadata `repo`, `page`, `commit`,
`run_id`, `mode`, `style` and the tags `docs-sync`, the repo name and `mode:<code|docs>`).

It is off by default because **it sends prompts, code excerpts and model output to a third party** (LangSmith cloud, or your own
self-hosted LangSmith). To enable it:

1. Create a LangSmith account and an API key.
2. `kubectl -n multirepo patch secret multisync-secrets --type merge -p '{"stringData":{"LANGSMITH_API_KEY":"<key>"}}'`
3. Set `LANGSMITH_TRACING: "true"` in the `multisync-config` ConfigMap (`infra/k8s/webhook.yaml`) and apply it. New Jobs pick it up.

No code change is needed: the Jobs read these variables through the same `envFrom` that carries the other settings.

## 3. Raw logs

`kubectl -n multirepo logs job/<name>` (kept five minutes after the Job ends) and `kubectl -n multirepo logs deploy/multisync-webhook`
(every accepted, ignored or rejected event). GitHub keeps each webhook delivery under Settings, Webhooks, Recent deliveries.

# Facts about a repository

`multisync.repo_facts` holds what is known about each repository as a whole, with provenance:

| Column | Meaning |
|---|---|
| `source_path` | the file the fact came from (`README.md`, `go.mod`, `Makefile`), or an aggregate: `@source-files`, `@workflows`, `@entrypoints` |
| `source_hash` | sha256 of that source when the fact was extracted (empty = unknown, counts as stale) |
| `extracted_at` | when |
| `verification_method` | `deterministic` (read from the repo, no model) or `llm_quote_grounded` (model output whose verbatim quote was found in the source; otherwise dropped) |
| `flag`, `flag_detail` | `contradicts_deterministic_source` when a README fact disagrees with a deterministic one |

**Staleness is checked wherever the facts are read** for the planner and the judge (`checked_repo_facts`): each fact's source is hashed again and
compared with `source_hash`; a file read and a hash, no model. Stale facts are **rebuilt inline** before use: the deterministic ones are
re-parsed (near free), and only a README whose content changed is sent to the cheap model again (only that README, not the whole source).
Without a model, stale README facts are left out rather than trusted. The result is logged in the `gar` node's note (`repoFacts`: facts,
stale, rebuilt, conflicts, excluded).

**Cross-check.** README facts are compared with deterministic ones where a category overlaps: the Go version against `go.mod`, "mainly written
in X" against the measured language mix, and framework major versions against `package.json`. A contradiction is flagged, and reaches the planner
as `CONFLICT, do not state: "..."` and the judge as a `CONFLICT:` line saying the deterministic value is true. Example from SuperGit: the README
says "Go 1.22+", `go.mod` declares Go 1.24.2.

```bash
python -m multisync.cli.factstore --repo-facts owner/name       # facts with their source, hash, time and flags
python -m multisync.cli.factstore --profile --repo owner/name --dir <checkout>
```
The page-level `claims` table is a different thing: claims are what a generated page asserts.
