# Seeing what the pipeline did

Three levels, from the one that always exists to the one you opt into. On top of level 2, a nightly job scores every draft for hallucination (see below).

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

## 2. Arize Phoenix: self-hosted traces (the LangGraph run as a tree)

Phoenix is open source and runs in the cluster (`infra/k8s/phoenix.yaml`, one container, stored in the existing Postgres under the schema
`phoenix`, about 0.5 GB of RAM). It is served at **https://rubenalejandrocalderoncorona.org/phoenix** behind its own login. Nothing leaves your
server. (Self-hosted LangSmith was ruled out: it needs an Enterprise license and 16 GB of RAM.)

Each run is one trace, grouped into sessions by run id, in the project `multirepo-agent-docs`:

```
docs-sync <repo> <page>                 the run: outcome, tier, cost
  node:prefilter, node:route, node:similarity, node:code_context, node:gar, node:write_draft, node:verify_draft, node:judge, ...
    llm:cheap deepseek-v4-pro           every model call: the exact messages, the answer, tokens, cost
    llm:expensive gpt-5.6-terra
    embeddings                          tokens per call
```
A failing node shows as an error span. Each node span carries that node's decision data (scores, tier, retrieved context, repo facts check).
The graph is traced with OpenTelemetry and the OpenInference span conventions (`multisync/tracing.py`); it is off unless
`PHOENIX_COLLECTOR_ENDPOINT` is set, and tracing can never fail a run.

**Access.** Log in as `admin@localhost` with the initial password in the cluster
(`kubectl -n multirepo get secret phoenix-secrets -o jsonpath='{.data.PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD}' | base64 -d`);
Phoenix asks for a new one at the first login. The Jobs send traces with a system key (`PHOENIX_API_KEY` in `multisync-secrets`); a new key
is made under Settings, System keys.

**Setup from scratch.** Create `phoenix-secrets` (the header of `phoenix.yaml` has the command), `CREATE SCHEMA phoenix` in the FactStore
database, apply `phoenix.yaml`, log in, create a system key, put it in `multisync-secrets` as `PHOENIX_API_KEY`. The traces contain prompts and
code excerpts, so keep the login strong.

### Draft scores (nightly evaluator)

Each attempt of `write_draft` also emits a `draft` span (the fact sheet and doc plan in, the draft out, the retrieved chunks as `retrieval.documents`, and a bounded record of the source
snapshot: `multisync.source.text` clipped to `DRAFT_SOURCE_CHARS`, default 30,000, omitted when `PHOENIX_CAPTURE_CONTENT=0`, plus hash and file paths). A nightly
CronJob scores those spans with the cheap-tier model against the same evidence the writer had and attaches a `draft_faithfulness` evaluation to each one (the share
of supported claims, 0..1: faithful >= 0.9, partial >= 0.7, unfaithful below; metadata has the counts, the unsupported quotes and `evidence: source+chunks | chunks_only`); `multisync evals
flag-low-scores` lists the low ones for a human. Before trusting the nightly job, run `python -m multisync.evals.run_draft_evals --sample 10 --dry-run` (evidence sizes, no model call) and `--sample 10 --explain` (scores with quotes, nothing written). It runs apart from generation and only reads and annotates in Phoenix. See [DRAFT-EVALS.md](DRAFT-EVALS.md).

### Review outcomes and confidence floors (every 6 hours)

What humans did with the drafts, next to the generation and judge traces, in the same Phoenix project (`multirepo-agent-docs`). Read-only on
`multisync.review_outcomes`; informational only (nothing auto-approves, `auto_approval_eligible` is never read).

| Span name | One per | What it carries |
|---|---|---|
| `review_outcome` | review row | `multisync.change_unit_id`, `multisync.pr_url`, `multisync.segment` (`<diff_classification>:<tier>`), `multisync.policy_version`, `multisync.outcome`, `multisync.reviewer`, `multisync.judge.<precision\|recall\|style\|quality>`, `multisync.synthetic`; `session.id` = the segment, so each segment is one session; span time = `reviewed_at` |
| `review_segment_metrics` | (segment, policy version, UTC date) | `multisync.n`, `no_edition_rate`, `wilson_lower_bound`/`wilson_upper_bound` (z 1.96), `graduation_progress` (n / min samples, capped at 1.0) and `graduation_n` (raw n), `ready`, `synthetic_n`, `judge_vs_human_correlation.<score>` (point-biserial vs. the unedited outcome, rejected excluded; absent when n < 5 or no variance). The same numbers are an annotation named `review_segment_metrics` (score = Wilson lower bound, label `ready`/`not_ready`), which Phoenix upserts, so the latest run of the day wins |

Filter in the Phoenix span filter box: `name == 'review_outcome'`, `metadata`-style attribute filters such as `attributes['multisync.segment'] == 'public_interface:expensive'`,
and `attributes['multisync.synthetic'] == False` to hide seeded rows (rows whose `pr_url` starts with `synthetic://` are exported but tagged `true`). Ids are derived
from (pr_url, change_unit_id) and (segment, policy, date), so re-running is idempotent: Phoenix drops spans it already has.

```bash
python -m multisync.cli.phoenix_review_evals --dry-run                 # per-segment table, no call to Phoenix
python -m multisync.cli.phoenix_review_evals --min-samples 30 --threshold 0.85 [--json]
multisync phoenix-review-evals --dry-run
```
Needs `FACTSTORE_DATABASE_URL`, `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY` and the `evals` extra. `infra/k8s/review-evals-cronjob.yaml` runs it every 6 hours.

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
