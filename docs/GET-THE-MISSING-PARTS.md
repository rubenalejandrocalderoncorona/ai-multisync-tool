# Getting the missing keys and variables

Repo: `rubenalejandrocalderoncorona/multirepo-agent-docs` (the central repo). Set values with `gh secret set NAME --repo ...` or in
Settings, Secrets and variables, Actions. Never paste a secret into a file in a repository.

## Already set
Secrets: `AI_API_KEY`, `DEEPSEEK_API_KEY`, `CAIMANDESK_API_TOKEN`, `FACTSTORE_DATABASE_URL`.
Variables: `QDRANT_URL`, `CAIMANDESK_PROJECT_ID=10`, `CAIMANDESK_URL`, `CAIMANDESK_PUBLIC_URL`, `CAIMANDESK_TRANSPORT=mcp`, `AI_EXPENSIVE_MODEL`, `AI_CHEAP_MODEL`.

## Still missing

| Name | Where | How to get it |
|---|---|---|
| `DOCS_SYNC_PAT` (secret, on the central repo **and** on each source repo) | GitHub, Settings, Developer settings, Personal access tokens, **Tokens (classic)** | Scopes `repo` and `workflow`. Classic because the source repo is in the `cAImanLabs` organization and the central repo is under your user; a fine-grained token has one owner. Do not reuse the `gh` login token. |
| Runner `ACCESS_TOKEN` (cluster secret) | GitHub, fine-grained token | Resource owner you, repository `multirepo-agent-docs`, permission **Administration: read and write**. On the VPS: `kubectl -n multirepo create secret generic multisync-runner --from-literal=ACCESS_TOKEN=...` |
| `multisync-secrets` (cluster secret) | VPS | `kubectl -n multirepo create secret generic multisync-secrets --from-literal=FACTSTORE_DATABASE_URL=... --from-literal=AI_API_KEY=... --from-literal=DEEPSEEK_API_KEY=...` (read by the migrate Job) |
| `RUNNER_LABEL=multisync` (variable) | central repo | Set it **only after** the runner shows as Idle in Settings, Actions, Runners; before that every job waits forever. Apply `infra/k8s` first. |
| `DEPLOY_ENABLED=true` (variable) | central repo | After the GHCR package for the site image is public (or the `ghcr-pull` secret from `deploy/k8s.yaml` exists). |
| `SLACK_WEBHOOK_URL` (optional secret) | Slack, Incoming Webhooks | Only for failure notices. |
| `sync-docs.yml` in the source repo | CalendarScheduler | Copy `examples/calendarscheduler/sync-docs.yml`, add `DOCS_SYNC_PAT` there. |
| Branch protection | central repo | Protect `main` and `qa`: pull requests only. |

## Rotate afterwards
The keys shared during development (OpenAI, DeepSeek, Postgres password, cAImanDesk token) should be rotated; update the GitHub
secrets and the cluster secrets with the new values.

Check everything with `node scripts/doctor.js`.
