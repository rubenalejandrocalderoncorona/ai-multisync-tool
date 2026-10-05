"""Setup doctor: shows exactly which credentials and endpoints are missing or unreachable.
Never prints a secret value. Exit code 1 if a REQUIRED item is missing or failing.

  python -m multisync.cli.doctor             check env + reach every service
  python -m multisync.cli.doctor --env-only  check env only (no network)
"""
from __future__ import annotations

import os
import re
import sys

import httpx

from ..config import load_config


def probe(fn) -> dict:
    try:
        return fn()
    except httpx.TimeoutException:
        return {"ok": False, "why": "unreachable/timeout"}
    except httpx.ConnectError:
        return {"ok": False, "why": "unreachable/timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": str(e)}


def main(argv: list[str]) -> int:
    env_only = "--env-only" in argv
    cfg = load_config()
    env = os.environ
    has = lambda k: bool(env.get(k))  # noqa: E731
    rows: list[dict] = []

    def add(area, item, required, status, hint=""):
        rows.append({"area": area, "item": item, "required": required, "status": status, "hint": hint})

    ai = cfg["ai"]
    # ── LLM / embeddings ───────────────────────────────────────────────────────
    ai_key = bool(ai["apiKey"])
    add("LLM", "INTERNAL_AI_API_KEY (or AI_API_KEY)", True, "set" if ai_key else "MISSING", "Provider API key for the writer/judge/embeddings")
    if ai_key and not env_only:
        r = probe(lambda: (lambda res: {"ok": res.is_success, "why": f"HTTP {res.status_code}"})(
            httpx.get(f"{ai['baseUrl']}/v1/models", headers={"Authorization": f"Bearer {ai['apiKey']}"}, timeout=8)))
        add("LLM", f"{ai['baseUrl']} reachable + key accepted", True, "ok" if r["ok"] else f"FAIL ({r['why']})", "Check AI_API_BASE_URL and the key")

        def emb():
            res = httpx.post(f"{ai['baseUrl']}{ai['embedPath']}", headers={"Authorization": f"Bearer {ai['apiKey']}"}, json={"model": ai["embedModel"], "input": ["ping"]}, timeout=10)
            if not res.is_success:
                return {"ok": False, "why": f"HTTP {res.status_code}"}
            dim = len(res.json()["data"][0]["embedding"])
            return {"ok": dim == ai["embedDim"], "why": f"model returns {dim} dims but AI_EMBED_DIM={ai['embedDim']}"}

        e = probe(emb)
        add("LLM", f"embeddings {ai['embedModel']} match AI_EMBED_DIM", True, "ok" if e["ok"] else f"FAIL ({e['why']})", "AI_EMBED_DIM must equal the model dimension")
    t = ai["tiers"]
    add("LLM", "expensive tier model", False, t["expensive"]["model"], "AI_EXPENSIVE_MODEL: used for public-interface and cross-repo changes and as the judge")
    ds_key = has("DEEPSEEK_API_KEY")
    add("LLM", "DEEPSEEK_API_KEY (cheap tier)", False, f"set ({t['cheap']['model']})" if ds_key else f"not set: cheap tier falls back to {t['cheap']['model']} on the primary provider",
        "DeepSeek drafts internals-only changes at a fraction of the cost")
    if ds_key and not env_only:
        r = probe(lambda: (lambda res: {"ok": res.is_success, "why": f"HTTP {res.status_code}"})(
            httpx.get(f"{t['cheap']['baseUrl']}/models", headers={"Authorization": f"Bearer {t['cheap']['apiKey']}"}, timeout=8)))
        add("LLM", f"{t['cheap']['baseUrl']} reachable + key accepted", False, "ok" if r["ok"] else f"FAIL ({r['why']})", "Check DEEPSEEK_API_KEY")

    # ── Qdrant ─────────────────────────────────────────────────────────────────
    q = cfg["qdrant"]
    if q["driver"] == "memory":
        add("Vector DB", "VECTOR_DRIVER=memory", False, "memory (nothing persists)", "Rehearsal only")
    else:
        add("Vector DB", "QDRANT_URL", True, "set" if has("QDRANT_URL") else "MISSING (default http://localhost:6333)", "URL of Qdrant on the VPS")
        add("Vector DB", "QDRANT_API_KEY", False, "set" if has("QDRANT_API_KEY") else "not set (fine if Qdrant is cluster-internal without auth)", "Only if your Qdrant requires an API key")
        if not env_only:
            r = probe(lambda: (lambda res: {"ok": res.is_success, "why": f"HTTP {res.status_code}"})(
                httpx.get(f"{q['url']}/collections", headers={"api-key": q["apiKey"]} if q["apiKey"] else {}, timeout=6)))
            add("Vector DB", f"Qdrant reachable ({q['collection']} + {q['codeCollection']})", True, "ok" if r["ok"] else f"FAIL ({r['why']})",
                "The in-cluster URL only resolves inside the cluster (the Jobs); from a laptop use an ssh tunnel")

    # ── FactStore ──────────────────────────────────────────────────────────────
    fs = cfg["factstore"]
    if fs["driver"] == "memory":
        add("FactStore", "FACTSTORE_DATABASE_URL", True, "MISSING (memory driver: cross-repo gate and audit log will not persist)", "postgres://user:pass@host:5432/factstore")
    else:
        add("FactStore", "FACTSTORE_DATABASE_URL", True, "set")
        if not env_only:
            def pg():
                import psycopg

                with psycopg.connect(fs["databaseUrl"], connect_timeout=6) as c:
                    c.execute("SELECT 1")
                return {"ok": True}

            p = probe(pg)
            add("FactStore", "Postgres reachable", True, "ok" if p["ok"] else f"FAIL ({p['why']})", "Postgres must be reachable from where the pipeline runs (the cluster Jobs)")

    # ── cAImanDesk ─────────────────────────────────────────────────────────────
    a = cfg["alerts"]
    if a["ticketProvider"] == "caimandesk":
        add("Tickets", "CAIMANDESK_API_TOKEN", True, "set" if a["deskToken"] else "MISSING", "cAImanDesk > Settings > API tokens (allow creating tasks)")
        add("Tickets", "CAIMANDESK_PROJECT_ID", True, "set" if a["deskProjectId"] else "MISSING", "Numeric id in the project URL, e.g. /projects/7")
        if not env_only and a["deskToken"] and a["deskProjectId"]:
            def desk():
                res = httpx.get(f"{a['deskBaseUrl']}/api/v1/projects/{a['deskProjectId']}", headers={"Authorization": f"Bearer {a['deskToken']}"}, timeout=8)
                extra = " (token rejected)" if res.status_code == 401 else " (project not found or no access)" if res.status_code == 404 else ""
                return {"ok": res.is_success, "why": f"HTTP {res.status_code}{extra}"}

            tr = probe(desk)
            add("Tickets", f"{a['deskBaseUrl']} project readable with token", True, "ok" if tr["ok"] else f"FAIL ({tr['why']})", "Token needs access to that project")
    else:
        add("Tickets", "TICKET_PROVIDER", False, a["ticketProvider"], "Set TICKET_PROVIDER=caimandesk for the demo")
    add("Tickets", "SLACK_WEBHOOK_URL", False, "set" if a["slackWebhook"] else "not set (optional)", "Optional alert channel")

    # ── GitHub ─────────────────────────────────────────────────────────────────
    add("GitHub", "DOCS_SYNC_PAT (cluster Secret / Actions secret)", bool(env.get("CI")), "set" if has("DOCS_SYNC_PAT") or has("GITHUB_TOKEN") else "MISSING (set it in the multisync-secrets Secret; not needed locally)",
        "Classic PAT with repo + workflow")

    # ── output ─────────────────────────────────────────────────────────────────
    wa, wi = max(len(r["area"]) for r in rows), max(len(r["item"]) for r in rows)
    bad = 0
    for r in rows:
        failing = r["required"] and re.search(r"MISSING|FAIL", r["status"])
        if failing:
            bad += 1
        mark = "✘" if failing else "!" if re.search(r"MISSING|FAIL", r["status"]) else "✔"
        hint = f"\n  {' ' * wa}  -> {r['hint']}" if failing and r["hint"] else ""
        print(f"{mark} {r['area'].ljust(wa)}  {r['item'].ljust(wi)}  {r['status']}{hint}")
    print(f"\n{f'{bad} required item(s) need attention. See docs/SETUP-REQUIRED.md.' if bad else 'All required items are in place.'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
