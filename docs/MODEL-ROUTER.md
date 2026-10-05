# Model router and FactStore coupling

Every run decides, **before any model call**, which model drafts the page. Nothing in the decision costs money.

| Step | Model |
|---|---|
| GAR rewrite, code and semantic analysis, planning | cheap (DeepSeek `deepseek-v4-pro`) |
| Retrieval | none: embedding cosine only (OpenAI `text-embedding-3-small`) |
| Draft | **routed**: cheap or expensive (OpenAI `gpt-5.6-terra`) |
| Verification | deterministic check, no model |
| Judge | expensive by default (`ROUTER_JUDGE=expensive|cheap|follow`) |

## How a draft is routed (`multisync/router.py`)

Expensive when any of these is true, otherwise cheap:
- a public interface changed (exports, routes, tRPC procedures, schema models/enums, env/config variables; compared by signature hash);
- a changed symbol is in the cross-repo `FEATURE_REGISTRY`;
- the FactStore `doc_refs` table shows docs in **other** repos or the site mention a changed symbol (code to docs coupling);
- a new page is created over public symbols.

`ROUTER_FORCE=cheap|expensive` overrides it. The reasons are stored in the decision (`tier`, `route`, `escalated`, `cost`).

## Verification cascade (`multisync/verify.py`)

A cheap draft must mention at least 60% of the changed public symbols (up to 40 checked), keep composable front matter, have a sane
length against the existing page, and keep its headings. A failure escalates **once** to the expensive model for that draft; it does
not use up an attempt and does not trigger a second model call to verify. Failures of an expensive draft go back to the writer as feedback.

## Models and cost

| Tier | Model | USD per 1M tokens (in/out) |
|---|---|---|
| cheap | DeepSeek `deepseek-v4-pro` | 0.66 / 1.98 |
| expensive | OpenAI `gpt-5.6-terra` | 2.0 / 12.0 |

`gpt-5.6-terra` rejects `temperature: 0`, so it is omitted for that model. DeepSeek has no embeddings, so these always use OpenAI.
Without `DEEPSEEK_API_KEY` the cheap tier uses `AI_FAST_MODEL` (default `gpt-4o-mini`) on OpenAI.
Live check on CalendarScheduler `overview.md`: cheap draft about $0.03, terra judge about $0.08, judge quality 0.95.

## FactStore (Postgres schema `multisync`)

`claims`, `decisions`, `node_logs`, `context_state`, plus the coupling tables `symbols` (public symbols per repo/file with signature hash)
and `doc_refs` (which docs mention which symbol). They are filled on every context sync, site sync and approval. To backfill or inspect:

```bash
python -m multisync.cli.factstore --migrate
python -m multisync.cli.factstore --backfill --repo owner/name --dir /path/to/checkout [--site-dir DIR --site-repo owner/central]
python -m multisync.cli.factstore --status
python -m multisync.cli.factstore --symbol DATABASE_URL
```
