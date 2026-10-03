# What you need to provide

Everything below is outside the repository: credentials, endpoints and a few GitHub settings. Nothing here is stored in code.
Run `node scripts/doctor.js` at any time; it checks each item (never printing a secret) and tells you what is still missing.

## 1. Infrastructure map

Red items are what is **not yet provided or not yet verified**. Green items exist.

```mermaid
flowchart LR
  subgraph GH["GitHub (rubenalejandrocalderoncorona / cAImanLabs)"]
    SRC["Source repos<br/>push to main"]
    SW["sync-docs-source.yml<br/>(in each source repo)"]
    CW["sync-docs-central.yml<br/>sync-docs-approved.yml<br/>(in caimanlabs-portfolio-docs)"]
    TOOL["ai-multysinc-tool<br/>(pipeline code, pinned by TOOL_REF)"]
    PR["Review PR<br/>docs-sync/*"]
    SITE["Starlight site<br/>caimanlabs-portfolio-docs"]
  end

  subgraph RUN["Runner"]
    R["Self-hosted runner on the VPS<br/>(or GitHub-hosted + tunnel)"]
  end

  subgraph VPS["Your VPS"]
    Q[("Qdrant<br/>approved doc chunks")]
    PG[("Postgres FactStore<br/>claims, decisions, node_logs")]
  end

  LLM["LLM provider (OpenAI-compatible)<br/>writer + judge + embeddings"]
  DESK["cAImanDesk<br/>tickets.caimanlabs.com.mx"]
  SLACK["Slack webhook (optional)"]
  HOST["Site hosting<br/>ghcr image + k8s ingress + domain"]

  SRC --> SW -->|"repository_dispatch<br/>DOCS_SYNC_PAT"| CW
  CW -->|"checkout"| TOOL
  CW --> R
  R -->|"QDRANT_URL + QDRANT_API_KEY"| Q
  R -->|"FACTSTORE_DATABASE_URL"| PG
  R -->|"INTERNAL_AI_API_KEY"| LLM
  R -->|"CAIMANDESK_API_TOKEN<br/>+ PROJECT_ID"| DESK
  R -.->|"SLACK_WEBHOOK_URL"| SLACK
  R -->|"auto trust: commit<br/>review trust: open PR"| PR
  PR -->|"merge = human approval<br/>index approved text"| Q
  PR --> SITE --> HOST

  classDef missing fill:#fde2e2,stroke:#c0392b,color:#7b1d12;
  classDef have fill:#e1f5e1,stroke:#2e7d32,color:#14401a;
  classDef partial fill:#fff4d6,stroke:#b8860b,color:#6b4e00;
  class Q,PG,LLM,R,SLACK,HOST missing;
  class DESK partial;
  class SRC,SW,CW,TOOL,PR,SITE have;
```

| Colour | Meaning |
|---|---|
| Red | Not provided yet, or code written but never run against the real service |
| Amber | The service exists and its API contract was verified; the token and project id are missing |
| Green | Exists in code / GitHub |

## 2. How one change flows (and which dependency each stage touches)

```mermaid
flowchart TD
  A["Push to a source repo"] --> B["prefilter<br/>no network"]
  B -->|"trivial or wording only"| X1["END: skipped<br/>0 tokens"]
  B --> C["cross_repo<br/>Postgres"]
  C -->|"registered feature not shipped everywhere"| F
  C --> D["similarity<br/>embeddings + Qdrant"]
  D -->|"shape unchanged and already documented"| X2["END: refreshed<br/>commit key updated, no writer/judge"]
  D --> E["gar<br/>LLM fast model: hypothetical paragraph"]
  E --> G["write_draft<br/>LLM + Qdrant context + style prompt"]
  G --> H["judge<br/>LLM as a judge: precision, recall, style, quality"]
  H -->|"grounded but awkward"| P["polish_draft<br/>language only"]
  P --> H
  H -->|"fail, retries left"| G
  H -->|"cap reached once"| W["widen<br/>top_k 5 to 12, one more round"]
  W --> G
  H -->|"pass"| U["publish"]
  H -->|"cap reached again"| F["fallback"]
  F --> T["Ticket in cAImanDesk<br/>+ Slack + rejected draft artifact"]
  U -->|"trust: auto"| I["commit + index in Qdrant"]
  U -->|"trust: review"| R["Pull request"]
  R -->|"merged"| I
  R -->|"closed unmerged"| X3["index untouched"]

  classDef llm fill:#e8e4ff,stroke:#5b43c9;
  classDef store fill:#e0f0ff,stroke:#1565c0;
  classDef stop fill:#eee,stroke:#777;
  classDef bad fill:#fde2e2,stroke:#c0392b;
  class E,G,H,P,W llm;
  class C,D,I store;
  class X1,X2,X3 stop;
  class F,T bad;
```

Every node writes one row to `node_logs` (Postgres) and one line to the runner log.

## 3. Credentials and endpoints you need to provide

### On the VPS (run once)

| # | Item | How |
|---|---|---|
| 1 | Docker + Compose on the VPS | install normally |
| 2 | `QDRANT_API_KEY`, `FACTSTORE_PASSWORD` | choose long random values; put in `.env` (copy `.env.example`) |
| 3 | Start the stack | `make stack-up`, then `make stack-check` |
| 4 | A way for CI to reach it | one of: a self-hosted GitHub runner on the VPS (recommended), WireGuard/Tailscale, or `--profile edge` for Qdrant over HTTPS (needs `QDRANT_DOMAIN`). Never publish Postgres |

### GitHub secrets and variables

Set these on **`caimanlabs-portfolio-docs`** (the central repo): Settings > Secrets and variables > Actions.

| Kind | Name | Required | Where to get it | Used by |
|---|---|---|---|---|
| secret | `DOCS_SYNC_PAT` | yes | GitHub > Settings > Developer settings > fine-grained token. Repos: the central repo (Contents, Pull requests, Workflows: write) and every source repo (Contents: read) | checkout of source repos, PR creation, issue fallback |
| secret | `AI_API_KEY` | yes | your LLM provider | writer, judge, embeddings |
| secret | `QDRANT_API_KEY` | yes | the value you chose in step 2 | vector store |
| secret | `FACTSTORE_DATABASE_URL` | yes | `postgres://factstore:<FACTSTORE_PASSWORD>@<host>:5432/factstore` | FactStore |
| secret | `CAIMANDESK_API_TOKEN` | yes | cAImanDesk > Settings > API tokens; allow creating tasks and comments | fallback tickets |
| secret | `SLACK_WEBHOOK_URL` | no | Slack incoming webhook | alerts |
| variable | `QDRANT_URL` | yes | e.g. `http://localhost:6333` on a VPS runner, or your HTTPS URL | vector store |
| variable | `CAIMANDESK_PROJECT_ID` | yes | number in the cAImanDesk project URL (`/projects/<id>`) | fallback tickets |
| variable | `RUNNER_LABEL` | if the runner is self-hosted | the label you gave the VPS runner | where jobs run |
| variable | `AI_API_BASE_URL`, `AI_MODEL`, `AI_FAST_MODEL`, `AI_EMBED_MODEL`, `AI_EMBED_DIM` | no | defaults: OpenAI, `gpt-4o`, `gpt-4o-mini`, `text-embedding-3-small`, `1536` | model choice. `AI_EMBED_DIM` must match the embedding model |
| variable | `TOOL_REPO`, `TOOL_REF` | no | defaults: `rubenalejandrocalderoncorona/ai-multysinc-tool`, `main` | pins the pipeline version |

In **each source repo**: secret `DOCS_SYNC_PAT` (a token that can dispatch to the central repo), and the copied `sync-docs-source.yml` with `CENTRAL_REPO`, `TARGET_BRANCH` and `SYNC_MODE` set.

### The judge

You said you will provide the judge. The pipeline only needs an OpenAI-compatible chat endpoint:

| Setting | Meaning |
|---|---|
| `AI_API_BASE_URL` + `AI_API_PATH` | endpoint (default `https://api.openai.com` + `/v1/chat/completions`) |
| `AI_MODEL` | the model used for the writer **and** the judge. To use a different, stronger model for judging only, tell me and I will add a `AI_JUDGE_MODEL` setting |
| prompts | already written: `prompts/judge-docs.md`, `prompts/judge-code.md` |

### Needed before the live demo can be called verified

| # | Item | Status |
|---|---|---|
| a | Qdrant and Postgres running on the VPS and reachable from the runner | not verified (the Docker daemon was off while building; the adapters are tested against fakes) |
| b | `similarity` threshold calibrated with real embeddings | default `0.85` is a guess; run the live demo and read `minChunkSimilarity` |
| c | judge thresholds calibrated on a few real pages | defaults `precision >= 0.90`, `recall >= 0.80`, `style >= 0.70`, `quality >= 0.75` |
| d | cAImanDesk token verified against the project | `node scripts/doctor.js` checks it |
| e | a domain and k8s ingress for the portfolio site | placeholders in `deploy/k8s.yaml` of the portfolio repo |

## 4. Order of operations

1. VPS: `make stack-up` and `make stack-check`.
2. Local: copy `.env.example` to `.env`, fill it, run `node scripts/doctor.js` until it is clean.
3. `npm run demo` (live) and read the similarity and judge numbers; adjust thresholds.
4. Set the GitHub secrets and variables above on the central repo.
5. Copy `sync-docs-source.yml` into one source repo, trigger it, watch the run.
