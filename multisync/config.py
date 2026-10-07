"""Central configuration. Everything is environment-driven (12-factor) so the same image runs under docker compose and Kubernetes.

Dict keys keep their JSON spelling (camelCase) because decisions and results are serialised and read by workflows (jq) and tests.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

from .util import AttrDict, attrs, read_json

__all__ = ["load_config", "repo_policy", "read_json"]


def _num(v: Any, default: float) -> float:
    if v is None or v == "":
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return int(n) if n == int(n) else n


def ai_config(env: Mapping[str, str], host: str) -> dict:
    """Two model tiers behind one client (see router.py for who goes where).

    cheap      bounded or low-risk work: GAR rewrites, template routing, drafts for internal-only changes
    expensive  drafts that touch a public interface, and the judge
    Embeddings are always the expensive provider's (OpenAI): DeepSeek has no embeddings API. Without DEEPSEEK_API_KEY the
    cheap tier falls back to a small model on the primary provider, so a single key still works.
    """
    primary_base = (env.get("AI_API_BASE_URL") or f"https://{host}").rstrip("/")
    primary_key = env.get("INTERNAL_AI_API_KEY") or env.get("AI_API_KEY") or ""
    ds_key = env.get("DEEPSEEK_API_KEY") or ""
    expensive_model = env.get("AI_EXPENSIVE_MODEL") or env.get("AI_MODEL") or "gpt-5.6-terra"
    tiers = {
        "expensive": {
            "name": "expensive", "baseUrl": primary_base, "chatPath": env.get("AI_API_PATH") or "/v1/chat/completions",
            "apiKey": primary_key, "model": expensive_model,
            # gpt-5.x rejects temperature 0 ("only the default (1) is supported"), so it is omitted for those models.
            "temperature": None if re.match(r"^gpt-5", expensive_model, re.I) or re.match(r"^o\d", expensive_model, re.I) else 0,
            "priceIn": _num(env.get("AI_EXPENSIVE_PRICE_IN"), 2.0), "priceOut": _num(env.get("AI_EXPENSIVE_PRICE_OUT"), 12.0),
        },
        "cheap": (
            {
                "name": "cheap", "baseUrl": (env.get("AI_CHEAP_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
                "chatPath": env.get("AI_CHEAP_PATH") or "/chat/completions", "apiKey": ds_key,
                "model": env.get("AI_CHEAP_MODEL") or "deepseek-v4-pro", "temperature": 0,
                "priceIn": _num(env.get("AI_CHEAP_PRICE_IN"), 0.66), "priceOut": _num(env.get("AI_CHEAP_PRICE_OUT"), 1.98),
            }
            if ds_key
            else {
                "name": "cheap", "baseUrl": primary_base, "chatPath": env.get("AI_API_PATH") or "/v1/chat/completions",
                "apiKey": primary_key, "model": env.get("AI_FAST_MODEL") or "gpt-4o-mini", "temperature": 0,
                "priceIn": _num(env.get("AI_CHEAP_PRICE_IN"), 0.15), "priceOut": _num(env.get("AI_CHEAP_PRICE_OUT"), 0.6),
            }
        ),
    }
    return {
        "apiKey": primary_key,
        "baseUrl": primary_base,
        "chatPath": tiers["expensive"]["chatPath"],
        "embedPath": env.get("AI_EMBED_PATH") or "/v1/embeddings",
        "model": expensive_model,
        "fastModel": tiers["cheap"]["model"],
        "embedModel": env.get("AI_EMBED_MODEL") or "text-embedding-3-small",
        "embedDim": _num(env.get("AI_EMBED_DIM"), 1536),
        "timeoutMs": _num(env.get("AI_TIMEOUT_MS"), 120000),
        "maxRetries": _num(env.get("AI_MAX_RETRIES"), 5),
        "tiers": tiers,
        # Which tier judges drafts: expensive (default, accuracy) | cheap | follow (same tier as the draft)
        "judgeTier": env.get("ROUTER_JUDGE") or "expensive",
        # auto | cheap | expensive: force every routed stage to one tier (tests and cost experiments)
        "routerForce": env.get("ROUTER_FORCE") or "auto",
    }


def load_config(env: Mapping[str, str] | None = None) -> AttrDict:
    env = os.environ if env is None else env
    host = env.get("AI_API_HOST") or "api.openai.com"
    g = env.get
    desk_url = (g("CAIMANDESK_URL") or "https://tickets.caimanlabs.com.mx").rstrip("/")
    cfg = {
        "ai": ai_config(env, host),
        "qdrant": {
            # qdrant | memory (memory: local rehearsal and tests only; nothing persists)
            "driver": g("VECTOR_DRIVER") or "qdrant",
            "url": (g("QDRANT_URL") or "http://localhost:6333").rstrip("/"),
            "apiKey": g("QDRANT_API_KEY") or "",
            "collection": g("QDRANT_COLLECTION") or "docs_chunks",  # semantic context: approved pages, source docs, page briefs
            "codeCollection": g("QDRANT_CODE_COLLECTION") or "code_context",  # code context: every in-scope source file, chunked
        },
        "factstore": {
            # postgres | memory (memory is for tests and the no-infra demo only)
            "driver": g("FACTSTORE_DRIVER") or ("postgres" if g("FACTSTORE_DATABASE_URL") else "memory"),
            "databaseUrl": g("FACTSTORE_DATABASE_URL") or "",
        },
        "thresholds": {
            "minDiffLines": _num(g("MIN_DIFF_LINES"), 3),
            "similarityHigh": _num(g("SIMILARITY_HIGH"), 0.92),
            "precisionMin": _num(g("PRECISION_MIN"), 0.9),
            "recallMin": _num(g("RECALL_MIN"), 0.85),
            "coreRecallMin": _num(g("CORE_RECALL_MIN"), 1),
            "styleMin": _num(g("STYLE_MIN"), 0.7),
            "judgeMin": _num(g("JUDGE_MIN"), 0.75),
            "maxIterations": _num(g("MAX_ITERATIONS"), 3),
            "topK": _num(g("TOP_K"), 5),
            "topKWidened": _num(g("TOP_K_WIDENED"), 12),
            "codeTopK": _num(g("CODE_TOP_K"), 12),
            "codeTopKWidened": _num(g("CODE_TOP_K_WIDENED"), 30),
            "contextBudgetChars": _num(g("CONTEXT_BUDGET_CHARS"), 30000),
            "patchDrafting": _num(g("PATCH_DRAFTING"), 1),  # 0 turns section-patch drafting off (always rewrite the whole page)
            "patchMaxChangedSectionShare": _num(g("PATCH_MAX_CHANGED_SECTION_SHARE"), 0.5),  # patch_too_broad above this share of sections ...
            "patchSmallChangeLines": _num(g("PATCH_SMALL_CHANGE_LINES"), 20),  # ... when the source diff is at most this many lines
            "patchGuardMinSections": _num(g("PATCH_GUARD_MIN_SECTIONS"), 4),  # pages with fewer sections are not guarded
            "patchDocsMode": _num(g("PATCH_DOCS_MODE"), 1),  # 0: an edited source doc is redrafted whole again (docs mode)
            "patchRevisions": _num(g("PATCH_REVISIONS"), 1),  # 0: the revise Job redrafts the whole page again
            "revisePatchMaxSectionShare": _num(g("REVISE_PATCH_MAX_SECTION_SHARE"), 1.0),  # drift guard share for revisions; 1.0 = guard off
            "contextMinScore": _num(g("CONTEXT_MIN_SCORE"), 0.45),  # retrieved chunks below this cosine score are dropped
        },
        # off | warn | block: the automatic linked-repos gate. Overrides a repo's own `crossRepoGate`; empty means "use the repo's, else block".
        "crossRepoGate": (g("CROSS_REPO_GATE") or "").strip().lower(),
        "paths": {
            "reposConfig": g("REPOS_CONFIG") or "config/repos.json",
            "featureRegistry": g("FEATURE_REGISTRY") or "config/feature-registry.json",
            "docsRoot": g("DOCS_ROOT") or "src/content/docs",
            "instructions": g("INSTRUCTIONS_FILE") or ".github/instructions/DocumentationInstructions.instructions.md",
            "templates": g("TEMPLATES_PATH") or "docs/templates",
            "styles": g("DOC_STYLES") or "",
        },
        "alerts": {
            "ticketProvider": g("TICKET_PROVIDER") or "caimandesk",  # caimandesk | github | jira | none
            # A ticket exists only for a draft waiting in QA (opened with the review PR). A fallback is logged (decision, node_logs, job summary,
            # Slack when configured) but opens no ticket unless TICKET_ON_FALLBACK=1.
            "ticketOnFallback": (g("TICKET_ON_FALLBACK") or "").lower() in ("1", "true", "yes"),
            "deskBaseUrl": desk_url,
            "deskToken": g("CAIMANDESK_API_TOKEN") or "",
            "deskProjectId": g("CAIMANDESK_PROJECT_ID") or "",
            # The base can be cluster-internal (http://caiman-tickets.caimanlabs-operations.svc.cluster.local); ticket links
            # always use the public URL.
            "deskPublicUrl": (g("CAIMANDESK_PUBLIC_URL") or g("CAIMANDESK_URL") or "https://tickets.caimanlabs.com.mx").rstrip("/"),
            # mcp (Vikunja's built-in MCP, the default) | rest (Vikunja API). Both use the same API token.
            "deskTransport": g("CAIMANDESK_TRANSPORT") or "mcp",
            "deskMcpUrl": g("CAIMANDESK_MCP_URL") or f"{desk_url}/api/v2/mcp",
            "slackWebhook": g("SLACK_WEBHOOK_URL") or "",
            "githubToken": g("GITHUB_TOKEN") or g("DOCS_SYNC_PAT") or "",
            "githubRepo": g("GITHUB_REPOSITORY") or "",
            "jiraBaseUrl": g("JIRA_BASE_URL") or "",
            "jiraEmail": g("JIRA_EMAIL") or "",
            "jiraToken": g("JIRA_API_TOKEN") or "",
            "jiraProject": g("JIRA_PROJECT_KEY") or "",
        },
    }
    return attrs(cfg)


def policy_version(cfg) -> str:
    """Identifies the review/generation policy a draft was produced under: the thresholds, the router and judge settings and the model names.
    Review outcomes are only comparable within one policy version, so it is stored with every outcome."""
    import hashlib
    import json as _json

    blob = _json.dumps({"t": dict(cfg["thresholds"]), "judge": cfg["ai"]["judgeTier"], "force": cfg["ai"]["routerForce"],
                        "models": {k: v["model"] for k, v in cfg["ai"]["tiers"].items()}}, sort_keys=True, default=str)
    return "v1-" + hashlib.sha256(blob.encode()).hexdigest()[:8]


def repo_policy(repos_config: Mapping[str, Any], repo_full_name: str) -> AttrDict:
    """Per-repo policy: trust level (auto|review), docs folder, style guide and glossary.

    Unknown repos get the safest defaults (review, no auto-publish)."""
    defaults = repos_config.get("defaults") or {}
    specific = (repos_config.get("repos") or {}).get(repo_full_name) or {}
    return attrs({
        "trust": "review",
        "serviceName": repo_full_name.split("/")[-1],
        "styleGuide": "",
        "glossary": {},
        **defaults,
        **specific,
    })
