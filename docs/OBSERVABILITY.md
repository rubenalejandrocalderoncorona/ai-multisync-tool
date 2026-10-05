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

`multisync.repo_facts` holds what is known about each repository as a whole: the main language and mix, the stack and frameworks, entry
points, build targets, CI workflows, the HTTP routes and environment variables found in the code (all read from the repository with no model),
and what the project is and does (extracted from the README by the cheap model, each fact with a verbatim quote that is checked against the
source; a fact whose quote is not found is dropped). The model step runs only when the README or manifests changed.

```bash
python -m multisync.cli.factstore --repo-facts owner/name
python -m multisync.cli.factstore --profile --repo owner/name --dir <checkout>      # rebuild now
```
The planner receives these facts as `REPO_FACTS`, and the judge treats them as known facts, so a page can say "mainly written in Go" without
being flagged as unsupported. The page-level `claims` table is a different thing: claims are what a generated page asserts.
