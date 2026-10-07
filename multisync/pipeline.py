"""The decision pipeline as a LangGraph StateGraph. Cheap-to-expensive, stopping early:

  START -> prefilter --skip-----------------------------------------> END
             `> cross_repo --blocked--> fallback --------------------> END
                   `> route > similarity --near-duplicate--> END (commit re-keyed)
                         `> [code mode: code_context (LLM stage 1)] > gar > [code mode: semantic_context (LLM stage 2)]
                               > write_draft > verify_draft > judge --pass--> publish > END
                                     ^              |fail
                                     |              |- grounded but awkward --> polish_draft --> judge
                                     |--------------| retries left
                                     `- widen ------| cap reached (once)
                                                    `- cap reached again ----> fallback > END

Every node is wrapped by `traced()`, which appends a structured event to `state["trail"]` and to the optional `deps.logger` (console +
FactStore node_logs), so each stage of each run is auditable and shows up in LangGraph's checkpointed history.
"""
from __future__ import annotations

import operator
import os
import posixpath
import re
import time
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import patching
from . import prompts as P
from . import tracing
from . import writer as W
from .chunker import chunk_markdown
from .context import retrieve_code, retrieve_semantic
from .coverage import check_coverage
from .critic import evaluate, judge as run_judge
from .prefilter import prefilter
from .registry import cross_repo_check
from .router import route_change
from .config import policy_version
from .repofacts import checked_repo_facts, facts_text, known_fact_lines
from .stages import analyze_code, plan_docs, plan_text, sheet_text
from .symbols import public_symbols
from .util import iso_now
from .structure import diff_line_count
from .verify import PATCH_TOO_BROAD, verify_draft

GROUNDED_ONLY_TAGS = {"style_mismatch", "judge_low_confidence"}


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    change: dict
    trail: Annotated[list, operator.add]
    attempts: Annotated[list, operator.add]
    metrics: Annotated[dict, _merge]
    ctx: Annotated[dict, _merge]
    forced: Any
    hypothetical: Any
    templatePath: Any
    knownFacts: Any
    styleText: Any
    iter: int
    widened: bool
    polished: bool
    feedback: list
    draft: Any
    verdict: Any
    failure: Any
    accepted: Any
    tier: str
    routeInfo: Any
    escalated: bool
    escalatePending: bool
    verifyFailed: bool
    garQueries: list
    relatedCode: str
    factSheet: Any
    repoFacts: Any
    plan: Any
    decision: Any
    patch: Any
    patchFallback: bool


def initial_state(change: dict) -> State:
    return {
        "change": change, "trail": [], "attempts": [], "metrics": {}, "ctx": {}, "forced": None, "hypothetical": None, "templatePath": None,
        "knownFacts": None, "styleText": None, "iter": 0, "widened": False, "polished": False, "feedback": [], "draft": None, "verdict": None,
        "failure": None, "accepted": None, "tier": "expensive", "routeInfo": None, "escalated": False, "escalatePending": False,
        "verifyFailed": False, "garQueries": [], "relatedCode": "", "factSheet": None, "repoFacts": [], "plan": None, "decision": None,
        "patch": None, "patchFallback": False,
    }


def target_path_for(file_path: str, policy, docs_root: str, subfolder: str | None = None) -> str:
    rel = re.sub(r"^(docs/|documentation/)", "", re.sub(r"\.txt$", ".md", file_path))
    base = policy.get("targetPath") or posixpath.join(docs_root, "services", policy["serviceName"])
    return posixpath.normpath(posixpath.join(base, subfolder, rel) if subfolder else posixpath.join(base, rel))


def _dedupe(chunks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in chunks:
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def _retrieve(vectors, llm, repo, after, hypothetical, gar_queries, top_k) -> list[dict]:
    vecs = llm.embed([after[:2000], hypothetical, *gar_queries])
    results = [vectors.search(v, limit=top_k, repo=repo) for v in vecs]
    rag, gars = results[0], results[1:]
    return [{"id": h["id"], "score": h["score"], "heading": h["payload"].get("heading"), "text": h["payload"].get("text")}
            for h in _dedupe([h for g in gars for h in g] + rag)[:top_k]]


def _usage_delta(a: dict | None, b: dict | None) -> dict | None:
    if not a or not b:
        return None
    out: dict = {}
    for t in ("cheap", "expensive"):
        d = {"calls": b[t]["calls"] - a[t]["calls"], "in": b[t]["in"] - a[t]["in"], "out": b[t]["out"] - a[t]["out"], "usd": round(b[t]["usd"] - a[t]["usd"], 5)}
        if d["calls"]:
            out[t] = d
    emb = b["embed"]["tokens"] - a["embed"]["tokens"]
    if emb:
        out["embedTokens"] = emb
    return out or None


def _cost_of(trail: list[dict]) -> dict:
    total = {"cheap": {"calls": 0, "in": 0, "out": 0, "usd": 0}, "expensive": {"calls": 0, "in": 0, "out": 0, "usd": 0}, "embedTokens": 0}
    for ev in trail or []:
        u = (ev.get("note") or {}).get("usage")
        if not u:
            continue
        for t in ("cheap", "expensive"):
            if t in u:
                for k in ("calls", "in", "out", "usd"):
                    total[t][k] += u[t][k]
        total["embedTokens"] += u.get("embedTokens", 0)
    total["usd"] = round(total["cheap"]["usd"] + total["expensive"]["usd"], 4)
    return total


def process_change(change: dict, deps) -> dict:
    """change: {repo, filePath, before, after (None = deleted), commit, kind?, ...}
    deps: {cfg, llm, vectors, facts, registry, policy, instructions, templateFiles, defaultTemplate, runId, githubHost?, logger?, escalate?,
           codeVectors?, styles?, siteRepo?}"""
    cfg, llm, vectors, facts, registry, policy, run_id = deps["cfg"], deps["llm"], deps["vectors"], deps["facts"], deps["registry"], deps["policy"], deps["runId"]
    code_vectors = deps.get("codeVectors") or vectors
    logger = deps.get("logger")
    t = cfg["thresholds"]
    base = {"runId": run_id, "repo": change["repo"], "path": change["filePath"], "commit": change["commit"]}
    mode = "code" if change.get("kind") == "code" else "docs"
    style = P.resolve_style(deps.get("styles") or P.load_styles(), change.get("styleKey") or policy.get("style"))
    style_txt = P.style_text(style, policy)
    changed_files = change.get("changedFiles") or []
    # Code mode writes to the style's outline under lean formatting standards; docs mode keeps the template + full standards.
    instructions = P.load_prompt("standards-code")["text"] if mode == "code" else deps.get("instructions")
    base["mode"] = mode
    base["style"] = style["key"]

    def traced(name, fn):
        """Wrap a node: time it, log it, append to the trail. A node returns {update, note, status}."""
        def run(state):
            started = time.time()
            usage0 = llm.snapshot_usage() if hasattr(llm, "snapshot_usage") else None
            with tracing.span(f"node:{name}", "CHAIN", **{"session.id": run_id, "multisync.node": name, "multisync.repo": change["repo"], "multisync.page": change["filePath"]}) as sp:
                result = _run_node(name, fn, state, started, usage0, sp)
            return result

        def _run_node(name, fn, state, started, usage0, sp):
            try:
                out = fn(state)
            except Exception as err:
                if logger:
                    logger.log({**base, "node": name, "status": "error", "ms": int((time.time() - started) * 1000), "note": {"error": str(err)}, "at": iso_now()})
                raise
            update, note, status = out.get("update") or {}, out.get("note") or {}, out.get("status") or "ok"
            usage = _usage_delta(usage0, llm.snapshot_usage() if hasattr(llm, "snapshot_usage") else None)
            event = {**base, "node": name, "status": status, "ms": int((time.time() - started) * 1000), "note": {**note, "usage": usage} if usage else note, "at": iso_now()}
            sp.set(**{"multisync.status": status, "multisync.ms": event["ms"]})
            sp.io(output=event["note"])
            if logger:
                logger.log(event)
            return {**update, "trail": [event]}
        return run

    def finish(state, d):
        ri = state.get("routeInfo")
        return {**base, "tier": state.get("tier"), "route": {"tier": ri["tier"], "reasons": ri["reasons"]} if ri else None,
                "escalated": bool(state.get("escalated")), "cost": _cost_of(state.get("trail")), "attempts": state.get("attempts") or [],
                "metrics": state.get("metrics") or {}, "context": {**(state.get("ctx") or {}), "gar": state.get("garQueries") or []}, "action": "none", **d}

    def with_metrics(s, m):
        return {**s, "metrics": {**s.get("metrics", {}), **m}}

    # ── nodes ────────────────────────────────────────────────────────────────────
    def n_prefilter(s):
        if deps.get("revision"):  # a reviewer asked for changes to an existing draft: the decision to write was already taken, never skip it as trivial
            return {"status": "skip", "note": {"reason": "revision after review"}, "update": {"forced": True}}
        if change.get("after") is None:
            auto = policy.get("trust") == "auto"
            return {"status": "deleted", "note": {"reason": "source document removed"},
                    "update": {"decision": finish(s, {
                        "outcome": "published" if auto else "pending_review", "reviewerAction": "auto_published" if auto else "needs_review",
                        "reason": "source document removed", "action": "delete",
                        "targetPath": target_path_for(change["filePath"], policy, cfg["paths"]["docsRoot"])})}}
        pre = prefilter(change.get("before") or "", change["after"], t["minDiffLines"])
        update = {"metrics": {"prefilter": pre["metrics"]}, "forced": pre["forced"]}
        if not pre["proceed"]:
            return {"status": "stop", "note": {"tag": pre.get("tag"), "reason": pre["reason"], **pre["metrics"]["diff"]},
                    "update": {**update, "decision": finish(with_metrics(s, update["metrics"]), {
                        "outcome": "skipped", "reviewerAction": "none", "rootCauseTag": pre.get("tag"), "reason": pre["reason"]})}}
        return {"note": {"reason": pre["reason"], "forced": pre["forced"], **pre["metrics"]["diff"]}, "update": update}

    def n_cross_repo(s):
        symbols = [x for x in registry if x in change["after"]]
        if not symbols:
            return {"status": "skip", "note": {"registered": []}}
        cross = cross_repo_check(symbols, change["repo"], registry, facts)
        update = {"metrics": {"crossRepo": cross}}
        if cross["complete"]:
            return {"note": {"registered": cross["registered"]}, "update": update}
        detail = "; ".join(f"{i['symbol']} awaiting {', '.join(i['missing'])}" for i in cross["incomplete"])
        return {"status": "stop", "note": {"incomplete": cross["incomplete"]},
                "update": {**update, "decision": finish(with_metrics(s, update["metrics"]), {
                    "outcome": "fallback", "reviewerAction": "auto_rejected", "rootCauseTag": "cross_repo_incomplete",
                    "reason": f"feature not available end to end: {detail}", "draft": None})}}

    def n_route(s):
        """Which model tier drafts this change. Free: no model call."""
        r = route_change(change, registry, facts, cfg["ai"]["routerForce"])
        return {"note": {"tier": r["tier"], "reasons": r["reasons"], "publicChanged": len(r["signals"]["publicChanged"]), "publicTotal": r["signals"]["total"],
                         "registryHits": len(r["signals"]["registry"]), "referencedBy": r["signals"]["referencedBy"]},
                "update": {"tier": deps.get("forceTier") or r["tier"], "routeInfo": r, "metrics": {"diffClassification": r["classification"]}, "ctx": {"route": {"tier": r["tier"], "reasons": r["reasons"]}}}}

    def n_similarity(s):
        hypothetical = None
        if mode == "code":
            # Code and prose are not comparable. Embed the GAR paragraph (what the docs *would* say) and compare it with what the page already says.
            hypothetical = W.generate_hypothetical(llm, file_path=change["filePath"], before=change.get("before"), after=change["after"], mode=mode, changed_files=changed_files)
            v = llm.embed([hypothetical])[0]
            hits = vectors.search(v, limit=1, repo=change["repo"], path=change["filePath"], kind="approved")
            best = [hits[0] if hits else {"id": None, "score": 0}]
        else:
            # Chunk-to-chunk, the same granularity as the index.
            new_chunks = chunk_markdown(change["after"])
            vecs = llm.embed([f"{c['heading']}\n{c['text']}" for c in new_chunks])
            best = []
            for v in vecs:
                hits = vectors.search(v, limit=1, repo=change["repo"], kind="approved")
                best.append(hits[0] if hits else {"id": None, "score": 0})
        low = min((b["score"] for b in best), default=0)
        update = {"metrics": {"minChunkSimilarity": low}, **({"hypothetical": hypothetical} if hypothetical else {})}
        note = {"mode": mode, "minChunkSimilarity": low, "chunks": len(best), "threshold": t["similarityHigh"], "forced": bool(s.get("forced"))}
        if not s.get("forced") and best and low >= t["similarityHigh"]:
            vectors.touch_commit([b["id"] for b in best if b.get("id")], change["commit"])
            what = "the page already says this" if mode == "code" else f"all {len(best)} chunk(s)"
            return {"status": "stop", "note": note,
                    "update": {**update, "decision": finish(with_metrics(s, update["metrics"]), {
                        "outcome": "refreshed", "reviewerAction": "none",
                        "reason": f"{what} (similarity {low:.3f} >= {t['similarityHigh']}); re-keyed to {change['commit'][:7]}"})}}
        return {"note": note, "update": update}

    def n_code_context(s):
        """STAGE 1 (code mode): LLM reads the changed code plus related code retrieved from the WHOLE repo index."""
        top_k = t["codeTopKWidened"] if s["widened"] else t["codeTopK"]
        in_snapshot = change.get("snapshotFiles") or []
        related = retrieve_code(llm=llm, store=code_vectors, repo=change["repo"], top_k=top_k, exclude=in_snapshot, budget_chars=t["contextBudgetChars"],
                                min_score=t["contextMinScore"],
                                queries=[change["after"][:3000], "\n".join(x for x in [change.get("brief"), style["key"], change["filePath"]] if x)])
        res = analyze_code(llm, page=change["filePath"], style_key=style["key"], changed_files=changed_files, repo_map=change.get("repoMap") or [],
                           code=change["after"], related=related, tier=s["tier"])
        sheet = res["sheet"]
        snap = change.get("snapshotFiles") or []
        return {
            "note": {"basedOnFiles": len(snap), "scopedFiles": len(change.get("repoMap") or []), "snapshotChars": len(change["after"]), "related": len(related),
                     "relatedFiles": len({c["path"] for c in related}),
                     "relatedTop": [f"{c['path']}:{c['start']}-{c['end']} ({c['score']:.2f})" for c in related[:6]], "facts": len(sheet["facts"]),
                     "droppedNoEvidence": res["dropped"], "unclear": len(sheet["unclear"]), "topK": top_k, "widened": s["widened"], "prompt": res["promptId"]},
            "update": {"relatedCode": "\n\n".join(c["text"] for c in related), "factSheet": sheet,
                       "ctx": {"snapshot": {"files": snap[:60], "count": len(snap), "chars": len(change["after"]), "scoped": len(change.get("repoMap") or [])},
                               "code": [{"path": c["path"], "start": c["start"], "end": c["end"], "score": round(c["score"], 3)} for c in related],
                               "facts": [{"id": f["id"], "text": f["text"], "evidence": f["evidence"]} for f in sheet["facts"]]}},
        }

    def n_semantic_context(s):
        """STAGE 2 (code mode): LLM decides what the page must contain from the fact sheet + the documentation index."""
        top_k = t["topKWidened"] if s["widened"] else t["topK"]
        related = retrieve_semantic(llm=llm, store=vectors, repo=change["repo"], top_k=top_k, site_repo=deps.get("siteRepo"), min_score=t["contextMinScore"],
                                    queries=[*s["garQueries"], s["hypothetical"], change.get("brief"), (change.get("existing") or "")[:1500]])
        if mode == "code":
            template = P.outline_text(style)
        elif s.get("templatePath") and os.path.exists(s["templatePath"]):
            template = open(s["templatePath"], encoding="utf-8").read()
        else:
            template = ""
        repo_facts = facts_text(s.get("repoFacts") or [])
        res = plan_docs(llm, sheet=s["factSheet"], brief=change.get("brief"), style_text=style_txt, existing=change.get("existing"), related=related, template=template,
                        tier=s["tier"], repo_facts=repo_facts)
        plan = res["plan"]
        return {
            "note": {"queries": len(s["garQueries"]) + 3, "garQueries": len(s["garQueries"]), "related": len(related), "kinds": list(dict.fromkeys(c["kind"] for c in related)),
                     "relatedTop": [f"{c['kind']}:{c['path']}{' > ' + c['heading'] if c.get('heading') else ''} ({c['score']:.2f})" for c in related[:6]],
                     "sections": len(plan["sections"]), "gaps": len(plan["gaps"]), "topK": top_k, "prompt": res["promptId"]},
            "update": {"plan": plan, "ctx": {"semantic": [{"kind": c["kind"], "path": c["path"], "heading": c.get("heading"), "score": round(c["score"], 3)} for c in related],
                                             "plan": [{"heading": x["heading"], "action": x["action"], "must_cover": x["must_cover"]} for x in plan["sections"]]}},
        }

    def n_gar(s):
        hypothetical = s.get("hypothetical") or W.generate_hypothetical(llm, file_path=change["filePath"], before=change.get("before"), after=change["after"], mode=mode, changed_files=changed_files)
        # Code mode: GAR proper. Hypothetical docs are written from the VERIFIED fact sheet (one paragraph per topic) and each one
        # becomes a query against the documentation index in the semantic stage.
        if mode == "code" and (s.get("factSheet") or {}).get("facts"):
            gar = W.generate_gar_from_facts(llm, sheet=s["factSheet"], brief=change.get("brief"))
        else:
            gar = {"paragraphs": []}
        template_path = None if mode == "code" else W.select_template(llm, file_path=change["filePath"], content=change["after"],
                                                                      template_files=deps.get("templateFiles") or [], default_template=deps.get("defaultTemplate"))
        known_facts = facts.approved_claims(change["repo"], change["filePath"])
        # Facts about the whole repository: each source is hashed again here, stale facts are rebuilt inline, and a README fact that contradicts
        # a deterministic one reaches the planner and the judge as an explicit CONFLICT.
        repo_rows, fact_report = [], None
        if hasattr(facts, "repo_facts"):
            if deps.get("repoGit") is not None:
                repo_rows, fact_report = checked_repo_facts(facts, change["repo"], change["commit"], deps["repoGit"], llm)
            else:
                repo_rows = facts.repo_facts(change["repo"])
            known_facts = [*known_facts, *known_fact_lines(repo_rows)]
        return {
            "note": {"template": os.path.basename(template_path) if template_path else f"outline:{style['key'] or 'none'}", "knownFacts": len(known_facts), "style": style["key"],
                     "styleFallback": style.get("fallback"), "reusedHypothetical": bool(s.get("hypothetical")), "garParagraphs": len(gar["paragraphs"]),
                     **({"garError": gar["error"]} if gar.get("error") else {}), **({"repoFacts": fact_report} if fact_report else {})},
            "update": {"hypothetical": hypothetical, "garQueries": gar["paragraphs"], "templatePath": template_path, "knownFacts": known_facts, "styleText": style_txt, "repoFacts": repo_rows},
        }

    def patch_base():
        """The existing page as the model owns it: no front matter, no Change History (code adds those). Empty when patching does not apply."""
        # Patch mode: incremental code run (a previous snapshot exists, so not a first draft and not a forced full sync) over an existing page.
        if mode != "code" or not t["patchDrafting"] or not change.get("before") or not (change.get("existing") or "").strip():
            return None
        base = patching.strip_generated(change["existing"])
        return base if len(patching.split_sections(base)) >= 2 else None

    def n_write_draft(s):
        top_k = t["topKWidened"] if s["widened"] else t["topK"]
        context = _retrieve(vectors, llm, change["repo"], change["after"], s["hypothetical"], s["garQueries"], top_k)
        common = dict(file_path=change["filePath"], source=change["after"], changed_files=changed_files, related_code=s["relatedCode"], fact_sheet=sheet_text(s["factSheet"]),
                      plan=plan_text(s["plan"]), context=context, policy=policy, style=style, instructions=instructions, feedback=[*(change.get("reviewFeedback") or []), *s["feedback"]], tier=s["tier"])
        note = {"attempt": s["iter"] + 1, "tier": s["tier"], "escalated": s["escalated"], "widened": s["widened"], "topK": top_k, "contextChunks": len(context),
                "feedbackItems": len(s["feedback"]), "style": style["key"]}
        base = None if s.get("patchFallback") else patch_base()
        metrics: dict = {"patchMode": False} if mode == "code" else {}
        patch_state = None
        out = None
        if base is not None:
            sections = patching.split_sections(base)
            try:
                names = ((s.get("routeInfo") or {}).get("signals") or {}).get("publicChanged") or []
                p_out = W.patch_document(llm, sections=sections, changed_symbols=names, diff_text=patching.source_diff(change.get("before"), change["after"], changed_files), **common)
                ch = p_out["changed"]
                n_changed = len(set(ch["replaced"]) | set(ch["deleted"]))
                out = {"text": p_out["text"], "promptId": p_out["promptId"]}
                patch_state = {"base": base, "sectionsTotal": len(sections), "sectionsChanged": n_changed, "diffLines": diff_line_count(change.get("before"), change["after"])["total"]}
                metrics = {"patchMode": True, "sectionsTotal": len(sections), "sectionsChanged": n_changed}
                note.update({"patchMode": True, "sectionsTotal": len(sections), "sectionsChanged": n_changed, "inserted": len(ch["inserted"]),
                             **({"unchangedReason": p_out["unchangedReason"]} if p_out["unchangedReason"] else {})})
            except (patching.PatchError, ValueError) as err:
                note.update({"patchFallback": True, "patchError": str(err)[:200]})
                metrics = {"patchMode": False, "patchFallback": True, "sectionsTotal": None, "sectionsChanged": None, "retainedPct": None}
        if out is None:
            out = W.draft_document(llm, mode=mode, existing=change.get("existing") or "", template_path=s.get("templatePath"), **common)
        update = {"iter": s["iter"] + 1, "polished": False, "draft": out["text"], "escalatePending": False, "verifyFailed": False, "patch": patch_state,
                  **({"patchFallback": True} if note.get("patchFallback") else {}), **({"metrics": metrics} if metrics else {})}
        return {"note": {**note, "prompt": out["promptId"]}, "update": update}

    def n_verify_draft(s):
        """Deterministic gate, no model call. A cheap-tier draft that fails is redone ONCE on the expensive tier (it does not use up an
        attempt); any other failure goes back to the writer with the exact reasons."""
        if change.get("before"):
            names = ((s.get("routeInfo") or {}).get("signals") or {}).get("publicChanged") or []
        else:
            # A page written from scratch: require the symbols that define the interface (routes, RPC procedures, schema models). Helpers, loggers and
            # environment variables are public to the symbol extractor but do not belong on every page; only when a repo has none of the former
            # (a library) are its exported names the interface.
            allsyms = list(public_symbols(change["after"]).values())
            core = [x["name"] for x in allsyms if x["kind"] in ("route", "rpc", "model")]
            names = core or [x["name"] for x in allsyms if x["kind"] == "export"]
        public_changed = names if mode == "code" else []
        plan = s.get("plan") or {}
        patch = ({**s["patch"], "maxShare": t["patchMaxChangedSectionShare"], "smallLines": t["patchSmallChangeLines"], "minSections": t["patchGuardMinSections"]}
                 if s.get("patch") else None)
        v = verify_draft(s["draft"], public_changed, change.get("existing") or "", change["filePath"], change.get("title") or None,
                         plan.get("purpose") or change.get("brief") or "Generated documentation.", patch=patch)
        note = {"tier": s["tier"], "ok": v["ok"], **v["metrics"], **({} if v["ok"] else {"reasons": v["reasons"][:4]})}
        retained = {"retainedPct": v["metrics"]["retainedPct"]} if "retainedPct" in v["metrics"] else {}
        cov = None
        if v["metrics"].get("mentioned"):
            got, total = (int(x) for x in v["metrics"]["mentioned"].split("/"))
            cov = round(100.0 * got / total, 1) if total else None
        if v["ok"]:
            return {"note": note, "update": {"verifyFailed": False, "metrics": {"symbolCoverage": cov, **retained}}}
        if s["tier"] == "cheap" and not s["escalated"]:
            return {"status": "escalate", "note": {**note, "escalatedTo": "expensive"},
                    "update": {"tier": "expensive", "escalated": True, "escalatePending": True, "feedback": v["reasons"], "iter": s["iter"] - 1}}  # the redo is free
        tag = PATCH_TOO_BROAD if any(r.startswith(PATCH_TOO_BROAD) for r in v["reasons"]) else "deterministic_check"
        attempt = {"n": s["iter"], "widened": s["widened"], "topK": t["topKWidened"] if s["widened"] else t["topK"], "failure": tag, "tier": s["tier"]}
        return {"status": "rejected", "note": note,
                "update": {"verifyFailed": True, "failure": {"tag": tag, "feedback": v["reasons"]}, "feedback": v["reasons"], "attempts": [attempt], "accepted": None,
                           "metrics": {"symbolCoverage": cov, **retained}}}

    def n_judge(s):
        judge_tier = s["tier"] if cfg["ai"]["judgeTier"] == "follow" else cfg["ai"]["judgeTier"]
        verdict = run_judge(llm, tier=judge_tier, mode=mode, source=change["after"], draft=s["draft"], known_facts=s["knownFacts"], style_text=s["styleText"],
                            existing=change.get("existing") or "", changed_files=changed_files, related_code=s["relatedCode"], fact_sheet=sheet_text(s["factSheet"]),
                            plan=plan_text(s["plan"]))
        failure = evaluate(verdict, t)
        # Deterministic completeness (code mode, styles that opt in): every declared name must be in the page.
        cov = check_coverage(s["draft"], change["after"], style.get("coverage")) if mode == "code" else None
        if cov and not cov["ok"]:
            names = [":".join(m.split(":")[1:]) for m in cov["missing"][:40]]
            total = sum(k["total"] for k in cov["kinds"].values())
            fb = f"Incomplete: {len(cov['missing'])} of {total} declared names are not documented. Add every one of: {', '.join(names)}{', ...' if len(cov['missing']) > 40 else ''}"
            failure = {**failure, "feedback": [*failure["feedback"], fb]} if failure else {"tag": "incomplete_coverage", "feedback": [fb]}
        polishable = bool(failure and failure["tag"] in GROUNDED_ONLY_TAGS and not s["polished"] and not s.get("patch"))
        scores = {"precision": verdict["precision"], "recall": verdict["recall"], "style": verdict["style"], "quality": verdict["quality"]}
        note = {**scores,
                **({"coverage": {k: f"{v['found']}/{v['total']}" for k, v in cov["kinds"].items()}, "coverageMissing": cov["missing"][:8]} if cov else {}),
                "failure": failure["tag"] if failure else None, "willPolish": polishable, "judgeTier": judge_tier, "prompt": verdict["promptId"], "coreRecall": verdict["coreRecall"],
                **({"missingCore": verdict["missingCore"][:6]} if verdict["missingCore"] else {}), **({"missing": verdict["missing"][:6]} if verdict["missing"] else {}),
                **({"unsupported": verdict["unsupported"][:6]} if verdict["unsupported"] else {})}
        if polishable:
            return {"note": note, "update": {"verdict": verdict, "failure": failure}}
        attempt = {"n": s["iter"], "widened": s["widened"], "topK": t["topKWidened"] if s["widened"] else t["topK"], **scores, "failure": failure["tag"] if failure else None}
        return {"note": note, "update": {"verdict": verdict, "failure": failure, "attempts": [attempt], "feedback": failure["feedback"] if failure else [],
                                         "accepted": None if failure else {"draft": s["draft"], "verdict": verdict}}}

    def n_polish_draft(s):
        draft = W.polish_only(llm, draft=s["draft"], policy=policy, style=style, instructions=instructions, tier=s["tier"])
        return {"note": {"reason": s["failure"]["tag"]}, "update": {"draft": draft, "polished": True}}

    def n_widen(s):
        return {"note": {"topK": t["topKWidened"], "reason": "iteration cap reached; one automatic retry with expanded context"}, "update": {"widened": True, "feedback": []}}

    def n_publish(s):
        draft, verdict = s["accepted"]["draft"], s["accepted"]["verdict"]
        # A code-mode page has a declared path (pages[].path), so it is never re-filed; docs-mode files are classified.
        subfolder = None if mode == "code" else W.classify_folder(llm, file_path=change["filePath"], content=draft, folder_spec=W.extract_folder_spec(deps.get("instructions")))
        host = deps.get("githubHost") or "github.com"
        source_url = (f"https://{host}/{change['repo']}/tree/{change['commit']}" if mode == "code" else f"https://{host}/{change['repo']}/blob/{change['commit']}/{change['filePath']}")
        if change.get("title"):
            title = change["title"]
        elif mode == "code":
            if change["filePath"] == "overview.md":
                title = policy["serviceName"]
            else:
                raw = re.sub(r"[-_/]", " ", re.sub(r"\.mdx?$", "", change["filePath"]))
                title = raw[:1].upper() + raw[1:]
        else:
            title = None
        plan = s.get("plan") or {}
        with_history = W.with_change_history(draft, repo=change["repo"], commit=change["commit"], gaps=plan.get("gaps") or [])
        content = W.with_frontmatter(with_history, file_path=change["filePath"], source_url=source_url, commit=change["commit"], doc_key=change["filePath"],
                                     title=title, description=plan.get("purpose") or change.get("brief") or None)
        facts.save_claims(change["repo"], change["filePath"], change["commit"], verdict["claims"])
        auto = policy.get("trust") == "auto"
        final = {"precision": verdict["precision"], "recall": verdict["recall"], "style": verdict["style"], "quality": verdict["quality"]}
        return {"note": {"trust": policy.get("trust"), "subfolder": subfolder, "claimsStored": len(verdict["claims"])},
                "update": {"decision": finish(s, {
                    "outcome": "published" if auto else "pending_review", "reviewerAction": "auto_published" if auto else "needs_review",
                    "reason": f"passed all checks in {len(s['attempts'])} attempt(s)", "action": "write", "content": content, "subfolder": subfolder,
                    "targetPath": target_path_for(change["filePath"], policy, cfg["paths"]["docsRoot"], subfolder),
                    "metrics": {**s["metrics"], "final": final, "policyVersion": policy_version(cfg), "modelTier": s["tier"], "escalated": bool(s["escalated"])}})}}

    def n_fallback(s):
        # Cross-repo blocks arrive with a decision already built; judge exhaustion builds it here.
        d = s.get("decision") or finish(s, {
            "outcome": "fallback", "reviewerAction": "auto_rejected", "rootCauseTag": "iteration_cap_exceeded",
            "reason": f"reflection loop did not converge in {len(s['attempts'])} attempt(s); last check: {(s.get('failure') or {}).get('tag')}",
            "draft": s.get("draft"), "feedback": (s.get("failure") or {}).get("feedback")})
        ticket = deps["escalate"](d) if deps.get("escalate") else None
        return {"status": "fallback", "note": {"rootCauseTag": d.get("rootCauseTag"), "ticket": ticket}, "update": {"decision": {**d, "ticket": ticket}}}

    # ── routing ──────────────────────────────────────────────────────────────────
    def after_judge(s):
        if s.get("accepted"):
            return "publish"
        if s.get("failure") and s["failure"]["tag"] in GROUNDED_ONLY_TAGS and not s["polished"] and not s.get("patch"):
            return "polish_draft"
        cap = t["maxIterations"] * 2 if s["widened"] else t["maxIterations"]
        if s["iter"] < cap:
            return "write_draft"
        return "fallback" if s["widened"] else "widen"

    def after_verify(s):
        if s["escalatePending"]:
            return "write_draft"
        return after_judge(s) if s["verifyFailed"] else "judge"

    g = StateGraph(State)
    for name, fn in [("prefilter", n_prefilter), ("cross_repo", n_cross_repo), ("route", n_route), ("similarity", n_similarity), ("code_context", n_code_context),
                     ("gar", n_gar), ("semantic_context", n_semantic_context), ("write_draft", n_write_draft), ("verify_draft", n_verify_draft),
                     ("judge", n_judge), ("polish_draft", n_polish_draft), ("widen", n_widen), ("publish", n_publish), ("fallback", n_fallback)]:
        g.add_node(name, traced(name, fn))
    g.add_edge(START, "prefilter")
    g.add_conditional_edges("prefilter", lambda s: END if s.get("decision") else "cross_repo", ["cross_repo", END])
    g.add_conditional_edges("cross_repo", lambda s: "fallback" if s.get("decision") else "route", ["fallback", "route"])
    g.add_edge("route", "similarity")
    g.add_conditional_edges("similarity", lambda s: END if s.get("decision") else ("code_context" if mode == "code" else "gar"), ["code_context", "gar", END])
    g.add_edge("code_context", "gar")
    g.add_conditional_edges("gar", lambda s: "semantic_context" if mode == "code" else "write_draft", ["semantic_context", "write_draft"])
    g.add_edge("semantic_context", "write_draft")
    g.add_edge("write_draft", "verify_draft")
    g.add_conditional_edges("verify_draft", after_verify, ["judge", "write_draft", "polish_draft", "widen", "fallback"])
    g.add_conditional_edges("judge", after_judge, ["publish", "polish_draft", "write_draft", "widen", "fallback"])
    g.add_edge("polish_draft", "judge")
    g.add_conditional_edges("widen", lambda s: "code_context" if mode == "code" else "write_draft", ["code_context", "write_draft"])  # re-read the whole context with a bigger budget
    g.add_edge("publish", END)
    g.add_edge("fallback", END)
    app = g.compile(checkpointer=MemorySaver())

    with tracing.span(f"docs-sync {change['repo']} {change['filePath']}", "AGENT", **{"session.id": run_id, "multisync.repo": change["repo"], "multisync.page": change["filePath"],
                                                                                      "multisync.commit": change["commit"], "multisync.mode": mode, "multisync.style": style["key"]}) as root:
        root.io(input={"repo": change["repo"], "page": change["filePath"], "commit": change["commit"], "mode": mode, "brief": change.get("brief")})
        final = app.invoke(initial_state(change), {
            "configurable": {"thread_id": f"{run_id}:{change['repo']}:{change['filePath']}"},
            "recursion_limit": int(25 + t["maxIterations"] * 8),
            "run_name": f"docs-sync {change['repo']} {change['filePath']}", "tags": ["docs-sync", change["repo"], f"mode:{mode}"],
            "metadata": {"repo": change["repo"], "page": change["filePath"], "commit": change["commit"], "run_id": run_id, "mode": mode, "style": style["key"]},
        })
        d = final["decision"]
        root.set(**{"multisync.outcome": d.get("outcome"), "multisync.tier": d.get("tier"), "multisync.escalated": bool(d.get("escalated")), "multisync.cost_usd": (d.get("cost") or {}).get("usd")})
        root.io(output=f"{d.get('outcome')}: {d.get('reason')}")
    return {**final["decision"], "trail": final["trail"]}
