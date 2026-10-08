"""Run the decision pipeline over the documents changed in a source repo checkout.

Modes (config/repos.json -> repos[repo].mode): 'docs' (default) syncs changed docs files; 'code' drafts pages from source-code
changes; 'both' does both.

Env:
  CHANGED_FILES   newline-separated doc paths (relative to source-repo/)
  SOURCE_REPO     org/repo            SOURCE_SHA   commit being synced
  SOURCE_BEFORE   previous commit (default: SOURCE_SHA~1)
  SOURCE_DIR      checkout dir (default: source-repo)
  FULL_SYNC       1 = (code mode) regenerate every declared page from the whole repo
  FORCE_PAGES     1 = regenerate the declared pages as new WITHOUT reloading the context (FULL_SYNC does both)
  ONLY_PAGES      comma list of page paths to regenerate (context is still loaded for every declared page)
  RUN_ID          correlation id (default: random)
  + AI_*, QDRANT_*, FACTSTORE_DATABASE_URL, see multisync/config.py

Writes: pipeline-results.json (decisions, no document bodies) and rejected/<file>.md drafts.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

from .. import gitutil as G
from .. import writer as W
from ..cli.onboard_check import plan_onboarding
from ..codesource import build_code_changes, pages_scope, redact_sensitive_paths
from ..config import repo_policy
from ..context import sync_context, sync_site
from ..fallback import escalate
from ..llm import usd_total
from ..pipeline import find_site_page, process_change
from ..prompts import load_styles
from ..repofacts import profile_repo
from ..sitedocs import read_site_file, site_commit, site_files
from .. import tracing
from ..util import iso_now
from ..wiring import apply_decision, build_deps


class OnboardingBlocked(RuntimeError):
    tag = "onboarding_blocked"


def main() -> None:
    tracing.configure()
    env = os.environ
    d = build_deps()
    cfg = d["cfg"]
    repo = env.get("SOURCE_REPO") or "unknown/unknown"
    source_dir = env.get("SOURCE_DIR") or "source-repo"
    commit = env.get("SOURCE_SHA") or G.head(source_dir)
    before = env.get("SOURCE_BEFORE") or f"{commit}~1"
    run_id = env.get("RUN_ID") or str(uuid.uuid4())
    files = (env.get("CHANGED_FILES") or "").split()

    if not cfg["ai"]["apiKey"] and not re.match(r"^https?://(localhost|127\.|ollama)", cfg["ai"]["baseUrl"]):
        raise RuntimeError("INTERNAL_AI_API_KEY is required (or point AI_API_BASE_URL at a local OpenAI-compatible server).")

    d["facts"].migrate()
    d["vectors"].ensure_collection()
    d["codeVectors"].ensure_collection()

    policy = repo_policy(d["reposConfig"], repo)
    # Never publish straight to a protected branch: when the target is main/master every change goes through a PR.
    if re.match(r"^(main|master)$", env.get("TARGET_BRANCH") or "") and policy.get("trust") == "auto":
        print(f"target branch {env.get('TARGET_BRANCH')} is protected: trust forced from auto to review")
        policy["trust"] = "review"
    instructions = open(cfg["paths"]["instructions"], encoding="utf-8").read() if os.path.exists(cfg["paths"]["instructions"]) else ""
    template_files = W.find_template_files(cfg["paths"]["templates"])
    default_template = os.path.join(cfg["paths"]["templates"], "default-template", "default-template.md")
    print(f"run {run_id} | {repo}@{commit[:7]} | trust={policy.get('trust')} | {len(files)} file(s)")

    styles = load_styles(cfg["paths"]["styles"])

    class Logger:
        """Console line for the job log + durable row in the FactStore for the audit trail."""

        def log(self, e: dict) -> None:
            print(f"    [{e['node']}] {e['status']} {e['ms']}ms {json.dumps(e.get('note'), default=str)}")
            try:
                d["facts"].record_node_log(e)
            except Exception as err:  # noqa: BLE001
                print(f"node log not persisted: {err}", file=sys.stderr)

    logger = Logger()
    escalate_fn = lambda dec: escalate(dec, cfg["alerts"])  # noqa: E731
    results: list[dict] = []
    os.makedirs("rejected", exist_ok=True)

    # CONTEXT STAGE: the vector DB must hold the whole repository before any change is analysed. If it cannot be loaded, code-mode
    # changes fall back (ticket, nothing published) instead of the run crashing. The rest of the documentation site is semantic
    # context too (terminology, structure, what is covered elsewhere).
    site_repo = env.get("SITE_REPO") or ""
    if site_repo and env.get("SITE_DIR"):
        t1 = time.time()
        base1 = {"runId": run_id, "repo": repo, "path": "*", "commit": commit, "node": "sync_site", "at": iso_now()}
        try:
            site = sync_site(site_repo=site_repo, commit=site_commit(env["SITE_DIR"]), files=site_files(env["SITE_DIR"]), read_file=read_site_file(env["SITE_DIR"]),
                             doc_store=d["vectors"], llm=d["llm"], facts=d["facts"])
            logger.log({**base1, "status": "skip" if site.get("skipped") else "ok", "ms": int((time.time() - t1) * 1000), "note": site})
        except Exception as e:  # noqa: BLE001 - degrades context only
            logger.log({**base1, "status": "error", "ms": int((time.time() - t1) * 1000), "note": {"error": str(e)}})

    context_error = None
    mode_setting = policy.get("mode")
    # The same free check as the onboarding dry run, enforced: a repository whose scope holds a file that looks sensitive (a secret-type path, a
    # "restricted" or "confidential" marker) is NOT sent to any model or embedded. Every page falls back with a ticket until the owner excludes the
    # file or acknowledges it with `allowSensitive` in config/repos.json.
    blocked = blocked_public = None  # blocked names the files (Job log, decision store); blocked_public never does (ticket, Slack, anything public)
    if mode_setting in ("code", "both"):
        try:
            plan_ = plan_onboarding(repo, policy, G.accessors(source_dir), commit, styles)
            blocked = "; ".join(plan_["blockers"]) or None
            blocked_public = "; ".join(plan_["blockersPublic"]) or None
        except Exception as e:  # noqa: BLE001 - a check that cannot run must not let the run send data it has not checked
            blocked = blocked_public = f"the onboarding check could not run: {redact_sensitive_paths(str(e))}"
        if blocked:
            print(f"onboarding blocked, nothing is embedded or sent to a model: {blocked}")
    if mode_setting in ("code", "both") and not blocked:
        t0 = time.time()
        base0 = {"runId": run_id, "repo": repo, "path": "*", "commit": commit, "node": "sync_context", "at": iso_now()}
        try:
            prev0 = before if G.rev_exists(source_dir, before) else ""
            stats = sync_context(repo=repo, commit=commit, before=prev0, full=env.get("FULL_SYNC") == "1", git=G.accessors(source_dir),
                                 code_store=d["codeVectors"], doc_store=d["vectors"], llm=d["llm"], facts=d["facts"], pages=policy.get("pages") or [],
                                 scope=pages_scope(policy.get("pages")), exclude=policy.get("exclude") or [], docs=policy.get("docs") or [])
            logger.log({**base0, "status": "ok", "ms": int((time.time() - t0) * 1000), "note": stats})
            t2 = time.time()
            base2 = {**base0, "node": "repo_facts", "at": iso_now()}
            try:
                pf = profile_repo(repo, commit, G.accessors(source_dir), d["facts"], d["llm"])
                logger.log({**base2, "status": "skip" if pf["llmSkipped"] else "ok", "ms": int((time.time() - t2) * 1000), "note": pf})
            except Exception as e:  # noqa: BLE001 - facts only enrich the context
                logger.log({**base2, "status": "error", "ms": int((time.time() - t2) * 1000), "note": {"error": str(e)}})
        except Exception as e:  # noqa: BLE001
            context_error = e
            logger.log({**base0, "status": "error", "ms": int((time.time() - t0) * 1000), "note": {"error": str(e)}})

    target_base = policy.get("targetPath") or os.path.join(cfg["paths"]["docsRoot"], "services", policy["serviceName"])
    changes: list[dict] = []
    if mode_setting != "code":
        for file in files:
            full = os.path.join(source_dir, file)
            unit = {
                "repo": repo, "filePath": file, "commit": commit, "kind": "docs", "before": G.read_at(source_dir, before, file),
                "after": open(full, encoding="utf-8").read() if os.path.exists(full) else None,
            }
            # An edited source doc whose site page already exists is patched, not converted again (new and deleted docs are not).
            page = find_site_page(target_base, file) if unit["after"] is not None and unit["before"] else None
            if page:
                unit.update(existing=re.sub(r"^---\n[\s\S]*?\n---\n+", "", open(page, encoding="utf-8").read(), count=1), existingPath=page,
                            **({"noPatch": True} if env.get("FULL_SYNC") == "1" or env.get("FORCE_PAGES") == "1" else {}))
            changes.append(unit)
    if mode_setting in ("code", "both"):
        prev = before if G.rev_exists(source_dir, before) else ""  # first commit / new branch => document everything in scope
        acc = G.accessors(source_dir)

        def read_existing(page: str) -> str:
            f = os.path.join(target_base, page)
            return re.sub(r"^---\n[\s\S]*?\n---\n+", "", open(f, encoding="utf-8").read(), count=1) if os.path.exists(f) else ""

        changes.extend(build_code_changes(repo=repo, policy=policy, commit=commit, before=prev, full=env.get("FULL_SYNC") == "1" or env.get("FORCE_PAGES") == "1",
                                          list_files=acc.list_files, read_at=acc.read_at, changed_between=acc.changed_between, read_existing_page=read_existing))
    # ONLY_PAGES=data-model.md,overview.md regenerates just those pages while the context stays loaded for ALL declared pages.
    only = [x.strip() for x in (env.get("ONLY_PAGES") or "").split(",") if x.strip()]
    if only:
        changes = [c for c in changes if not (c["kind"] == "code" and c["filePath"] not in only)]
    print(f"mode={mode_setting or 'docs'} | {len(changes)} change unit(s){' (only: ' + ', '.join(only) + ')' if only else ''}")

    run_ceiling = cfg["thresholds"]["maxRunUsd"]
    run_spend = lambda: usd_total(d["llm"].usage) if hasattr(d["llm"], "usage") else 0.0  # noqa: E731
    not_attempted = 0

    for change in changes:
        file = change["filePath"]
        if run_ceiling and run_spend() >= run_ceiling:
            # Run ceiling reached: no model call for the rest. The pages are visible as fallbacks (no ticket) and can be re-run with ONLY_PAGES.
            not_attempted += 1
            decision = {"runId": run_id, "repo": repo, "path": file, "commit": commit, "outcome": "fallback", "reviewerAction": "auto_rejected",
                        "rootCauseTag": "cost_ceiling_run", "attempts": [], "metrics": {"costUsd": 0}, "action": "none", "trail": [], "ticket": None,
                        "reason": f"not attempted: run spent ${run_spend():.2f} of the ${run_ceiling:g} run ceiling"}
            apply_decision(decision, d, repo, file, commit)
            d["facts"].record_decision(decision)
            results.append({k: v for k, v in decision.items() if k != "trail"} | {"stages": []})
            print(f"  {decision['outcome'].ljust(14)} {file}  — {decision['reason']} [cost_ceiling_run]")
            continue
        try:
            if blocked and change["kind"] == "code":
                raise OnboardingBlocked(blocked_public)
            if context_error and change["kind"] == "code":
                raise RuntimeError(f"context sync failed: {context_error}")
            decision = process_change(change, {
                "styles": styles, "cfg": cfg, "llm": d["llm"], "vectors": d["vectors"], "codeVectors": d["codeVectors"], "facts": d["facts"], "registry": d["registry"], "reposConfig": d["reposConfig"],
                "policy": policy, "instructions": instructions, "templateFiles": template_files, "defaultTemplate": default_template, "runId": run_id,
                "githubHost": env.get("GIT_HOST"), "repoGit": G.accessors(source_dir), "logger": logger, "escalate": escalate_fn, "siteRepo": site_repo or None,
            })
        except Exception as e:  # noqa: BLE001 - infrastructure failure (AI/Qdrant/Postgres down): fail safe, never publish
            decision = {"runId": run_id, "repo": repo, "path": file, "commit": commit, "outcome": "fallback", "reviewerAction": "auto_rejected",
                        "rootCauseTag": getattr(e, "tag", "pipeline_error"), "reason": str(e), "attempts": [],
                        "metrics": {"onboardingBlockers": blocked} if isinstance(e, OnboardingBlocked) else {}, "action": "none", "trail": []}
            decision["ticket"] = escalate(decision, cfg["alerts"])

        apply_decision(decision, d, repo, file, commit)
        d["facts"].record_decision(decision)
        slim = {k: v for k, v in decision.items() if k not in ("content", "draft", "trail")}
        slim["costUsd"] = (decision.get("metrics") or {}).get("costUsd", (decision.get("cost") or {}).get("usd"))
        slim["stages"] = [f"{t['node']}:{t['status']}" for t in decision.get("trail") or []]
        results.append(slim)
        tag = f" [{decision['rootCauseTag']}]" if decision.get("rootCauseTag") else ""
        print(f"  {decision['outcome'].ljust(14)} {file}  — {decision['reason']}{tag}")

    with open("pipeline-results.json", "w", encoding="utf-8") as fh:
        json.dump({"runId": run_id, "repo": repo, "commit": commit, "trust": policy.get("trust"), "results": results}, fh, indent=2, default=str)
    count = lambda o: sum(1 for r in results if r["outcome"] == o)  # noqa: E731
    print(f"\npublished {count('published')} | pending_review {count('pending_review')} | refreshed {count('refreshed')} | skipped {count('skipped')} | fallback {count('fallback')}")
    spent = [f"{r['path']} ${r['costUsd']:.2f}" for r in results if r.get("costUsd")]
    print(f"spend: run ${run_spend():.2f}" + (f" (ceiling ${run_ceiling:g})" if run_ceiling else "") + (" | per page: " + ", ".join(spent) if spent else ""))
    if not_attempted:
        print(f"{not_attempted} pages not attempted: run ceiling ${run_ceiling:g} reached (re-run them with ONLY_PAGES=<page paths>)")
    d["facts"].close()
    tracing.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
