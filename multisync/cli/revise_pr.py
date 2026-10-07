"""Review feedback loop: redraft the pages of a bot-authored docs-sync PR after a reviewer submitted "Request changes".

Run by the `revise` Job (scripts/job-entrypoint.sh) in three calls, so that nothing is announced before it is pushed:

  python -m multisync.cli.revise_pr --plan    guards only; prints {"proceed": bool, ...} (repo and commit the PR was drafted from, its pages)
  python -m multisync.cli.revise_pr           redrafts each page with the review as feedback and the CURRENT branch tip as the base, writes the
                                              pages that passed the gates, writes revise-result.json, comments itself only when nothing is pushed
  python -m multisync.cli.revise_pr --post    after the push: the one `<!-- multisync:revised review=ID -->` comment

Guardrails (all checked here, from the GitHub API, not from the webhook payload): the review is for this PR and is "changes requested"; the PR is open,
bot-authored, a docs-sync PR into the target branch and not superseded; the reviewer is not a bot; at most MAX_ROUNDS rounds per PR; one run per review id.
It never merges, approves, dismisses or force-pushes, and writes only the files the PR already contains. Pushing is done by git in the entrypoint.

env: CENTRAL_REPO PR_NUMBER REVIEW_ID TARGET_BRANCH GITHUB_TOKEN  + (work phase) SOURCE_DIR REPOS_CONFIG DOC_STYLES ... as for run_pipeline
"""
from __future__ import annotations

import json
import os
import re
import sys

from ..codesource import build_code_changes
from ..review_outcomes import SUPERSEDED_MARKER, GitHub, parse_pr_body

MAX_ROUNDS = 3
MAX_COMMENTS = 30
MAX_COMMENT_CHARS = 2000
RESULT_FILE = "revise-result.json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_@+=,. /-]+$")
ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
ROUND_RE = re.compile(r"<!-- multisync:(?:revised|revise-failed) review=(\d+) -->")


def marker(kind: str, review_id: int | str) -> str:
    """kind: revised (pushed), revise-failed (no safe revision), revise-capped (round limit reached)."""
    return f"<!-- multisync:{kind} review={review_id} -->"


def is_bot(user: dict | None) -> bool:
    u = user or {}
    return str(u.get("login", "")).endswith("[bot]") or u.get("type") == "Bot"


def guard(gh, number: int, review_id: int, base_branch: str, post_cap: bool = True) -> dict:
    """{proceed: False, reason} when this review must not be acted on, else {proceed: True, pr, review, info}."""
    comments = gh.issue_comments(number)
    bodies = [c.get("body") or "" for c in comments]
    if any(re.search(rf"<!-- multisync:(?:revised|revise-failed|revise-capped) review={review_id} -->", b) for b in bodies):
        return {"proceed": False, "reason": f"review {review_id} was already handled"}
    pr = gh.pr(number)
    head_ref = (pr.get("head") or {}).get("ref", "")
    if pr.get("state") != "open" or pr.get("merged"):
        return {"proceed": False, "reason": "the pull request is not open"}
    if not head_ref.startswith("docs-sync/") or (pr.get("base") or {}).get("ref") != base_branch:
        return {"proceed": False, "reason": f"not a docs-sync pull request into {base_branch}"}
    if not str((pr.get("user") or {}).get("login", "")).endswith("[bot]"):
        return {"proceed": False, "reason": "the pull request was not opened by the bot"}
    if SUPERSEDED_MARKER in (pr.get("body") or "") or any(SUPERSEDED_MARKER in b for b in bodies):
        return {"proceed": False, "reason": "the pull request was superseded by a newer draft"}
    info = parse_pr_body(pr.get("body"))
    if not info or not REPO_RE.match(info["repo"]) or not SHA_RE.match(info["sha"]):
        return {"proceed": False, "reason": "the pull request body is not a docs-sync body"}
    review = gh.review(number, review_id)
    if str(review.get("state", "")).upper() != "CHANGES_REQUESTED":
        return {"proceed": False, "reason": "the review is not a request for changes"}
    if is_bot(review.get("user")):
        return {"proceed": False, "reason": "the reviewer is a bot"}
    if review.get("author_association") not in ASSOCIATIONS:
        return {"proceed": False, "reason": "the reviewer is not an owner, member or collaborator"}
    rounds = {m for b in bodies for m in ROUND_RE.findall(b)}
    if len(rounds) >= MAX_ROUNDS:
        if post_cap:
            gh.comment(number, f"{marker('revise-capped', review_id)}\nThis pull request already went through {MAX_ROUNDS} automatic revision rounds. "
                               "No further automatic revision will be made: please edit the pages by hand on this branch.")
        return {"proceed": False, "reason": f"round limit of {MAX_ROUNDS} reached"}
    return {"proceed": True, "pr": pr, "review": review, "info": info}


def clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:MAX_COMMENT_CHARS] + (" ..." if len(text) > MAX_COMMENT_CHARS else "")


def match_page(page: str, files: list[str]) -> str | None:
    """The PR file holding a page: the page path itself or the path below a folder; docs-mode pages lose their docs/ prefix on the site."""
    rel = re.sub(r"^(docs|documentation)/", "", page)
    for cand in (page, rel):
        hit = next((f for f in files if f == cand or f.endswith("/" + cand)), None)
        if hit and PATH_RE.match(hit) and ".." not in hit and not hit.startswith("/"):
            return hit
    return None


def collect_feedback(review: dict, comments: list[dict], page_files: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """Feedback per page, plus what cannot be acted on. The review body and comments without a usable file apply to every page; a comment on a PR
    file that is not a page, or on a file outside the PR, is reported back as not acted on."""
    who = (review.get("user") or {}).get("login", "reviewer")
    general, per_page, skipped = [], {p: [] for p in page_files}, []
    if clip(review.get("body") or ""):
        general.append(f"Review by {who}: {clip(review['body'])}")
    for c in comments[:MAX_COMMENTS]:
        text = clip(c.get("body") or "")
        if not text:
            continue
        path, line = c.get("path"), c.get("line") or c.get("original_line")
        where = f"{path}:{line}" if path and line else (path or "")
        page = next((p for p, f in page_files.items() if f == path), None)
        if page:
            per_page[page].append(f"Comment on {where}: {text}")
        elif path:
            skipped.append(f"comment on `{path}` (not a page of this pull request): {text[:200]}")
        else:
            general.append(f"Comment: {text}")
    if len(comments) > MAX_COMMENTS:
        skipped.append(f"{len(comments) - MAX_COMMENTS} further inline comment(s) beyond the first {MAX_COMMENTS}")
    return {p: [*general, *per_page[p]] for p in page_files}, skipped


def strip_front(text: str) -> str:
    return re.sub(r"^---\n[\s\S]*?\n---\n+", "", text, count=1)


def page_change(*, repo, commit, page, target, policy, acc, root, feedback) -> dict | None:
    """The change unit for one page, rebuilt from the whole source at the original commit; `existing` is the page as it is NOW on the branch."""
    existing = strip_front(open(os.path.join(root, target), encoding="utf-8").read())
    declared = next((p for p in policy.get("pages") or [] if p["path"] == page), None)
    if declared and policy.get("mode") in ("code", "both"):
        units = build_code_changes(repo=repo, policy={**policy, "pages": [declared]}, commit=commit, before="", full=True, list_files=acc.list_files,
                                   read_at=acc.read_at, changed_between=acc.changed_between, read_existing_page=lambda _p: existing)
        unit = units[0] if units else None
    else:  # docs mode: the page is a markdown file of the source repository
        rel = re.sub(r"^(docs|documentation)/", "", page)
        text = next((t for t in (acc.read_at(commit, c) for c in (page, f"docs/{rel}", f"documentation/{rel}")) if t is not None), None)
        unit = {"repo": repo, "filePath": page, "commit": commit, "kind": "docs", "before": None, "after": text, "existing": existing} if text is not None else None
    if unit:
        # revisionPatch: patch the page at the branch tip with the reviewer's comments as the change (falls back to a full redraft if that fails)
        unit.update({"commit": commit, "existing": existing, "reviewFeedback": feedback, "revisionPatch": True})
    return unit


def scores_of(d: dict) -> dict:
    f = (d.get("metrics") or {}).get("final") or {}
    return {k: f.get(k) for k in ("precision", "recall", "style", "quality")}


def run_revision(gh, *, number: int, review_id: int, base_branch: str, deps: dict, acc, root: str, commit: str, logger=None) -> dict:
    """Redraft every page of the PR; write the ones that pass the pipeline gates (judge scores and deterministic checks). Returns the result record.
    Writes only files that the PR already contains. Nothing is committed, pushed or recorded in review_outcomes here."""
    from ..pipeline import process_change  # imported late: the guard-only calls need neither langgraph nor a model

    g = guard(gh, number, review_id, base_branch)
    if not g["proceed"]:
        return {"status": "skipped", "reason": g["reason"], "review_id": review_id, "pages": [], "not_acted": [], "changed": []}
    info, review = g["info"], g["review"]
    files = gh.files(number)
    page_files, not_acted = {}, []
    for page in info["pages"]:
        target = match_page(page, files)
        if target and os.path.isfile(os.path.join(root, target)):
            page_files[page] = target
        else:
            not_acted.append(f"`{page}`: not found among the files of this pull request")
    feedback, skipped = collect_feedback(review, gh.review_comments(number, review_id), page_files)
    not_acted += skipped
    result = {"status": "failed", "review_id": review_id, "reviewer": (review.get("user") or {}).get("login"), "repo": info["repo"], "commit": commit, "pages": [],
              "not_acted": not_acted, "changed": []}
    if not any(feedback.values()):
        result["status"] = "noop"
        result["reason"] = "the review has no text and no inline comments to act on"
        return result
    for page, target in page_files.items():
        entry = {"page": page, "path": target, "status": "unchanged"}
        result["pages"].append(entry)
        d_stored = deps["facts"].find_decision(info["repo"], info["sha"], page)
        tier = ((d_stored or {}).get("metrics") or {}).get("modelTier")
        entry["tier"] = tier
        change = page_change(repo=info["repo"], commit=commit, page=page, target=target, policy=deps["policy"], acc=acc, root=root, feedback=feedback[page])
        if change is None:
            entry.update(status="failed", reason="the source files of this page are not available at the original commit")
            continue
        decision = process_change(change, {**deps, "revision": True, "forceTier": tier if tier in ("cheap", "expensive") else None, "logger": logger,
                                           "escalate": None, "runId": f"revise-{review_id}"})
        entry["attempts"] = len(decision.get("attempts") or [])
        m = decision.get("metrics") or {}
        entry["draftMode"] = m.get("draftMode")
        if m.get("patchFallback"):
            entry["patchFallback"] = True
        if decision.get("action") != "write" or decision.get("outcome") not in ("pending_review", "published"):
            fails = [a.get("failure") for a in decision.get("attempts") or [] if a.get("failure")]
            entry.update(status="failed", reason=decision.get("reason"), tag=decision.get("rootCauseTag"), failed_checks=sorted(set(fails)),
                         feedback=[str(x)[:300] for x in (decision.get("feedback") or [])][:5])
            continue
        with open(os.path.join(root, target), "w", encoding="utf-8") as fh:
            fh.write(decision["content"])
        entry.update(status="revised", scores=scores_of(decision), tier=decision.get("tier") or tier)
        result["changed"].append(target)
    if result["changed"]:
        result["status"] = "revised"
    elif all(e["status"] == "unchanged" for e in result["pages"]):
        result["status"] = "noop"
    return result


def render_comment(result: dict, head_sha: str = "") -> str:
    rid = result["review_id"]
    lines = []
    if result["status"] == "revised":
        lines += [marker("revised", rid), f"Revised after review {rid} (by @{result.get('reviewer')})" + (f", pushed as `{head_sha[:7]}`." if head_sha else "."), "",
                  "| Page | Result | Precision | Recall | Style | Quality | Tier |", "|---|---|---|---|---|---|---|"]
        for e in result["pages"]:
            s = e.get("scores") or {}
            lines.append(f"| `{e['page']}` | {e['status']} | {s.get('precision', '')} | {s.get('recall', '')} | {s.get('style', '')} | {s.get('quality', '')} | {e.get('tier') or ''} |")
        modes = [f"- `{e['page']}`: {e['draftMode']}" for e in result["pages"] if e.get("draftMode") and e["status"] == "revised"]
        lines += ["", "Each revised page was revised from the page as it stood on this branch, with your comments as the change, and passed the same judge and deterministic checks as the first draft."]
        if modes:
            lines += ["", "Draft mode:", *modes]
    else:
        lines += [marker("revise-failed", rid), f"No safe revision could be produced for review {rid}, so nothing was pushed.", ""]
        for e in result["pages"]:
            if e["status"] == "failed":
                checks = ", ".join(e.get("failed_checks") or []) or e.get("tag") or "unknown"
                lines.append(f"- `{e['page']}`: {e.get('reason')} (failed checks: {checks})")
                lines += [f"  - {f}" for f in e.get("feedback") or []]
        if result["status"] == "noop":
            lines.append(f"- {result.get('reason') or 'nothing to change'}")
        lines += ["", "Please edit the pages by hand on this branch, or submit a new review with more specific comments."]
    failed = [e for e in result["pages"] if e["status"] == "failed"] if result["status"] == "revised" else []
    skipped = [*(f"`{e['page']}`: no safe revision ({e.get('reason')})" for e in failed), *result.get("not_acted", [])]
    if skipped:
        lines += ["", "Not changed:", *[f"- {x}" for x in skipped]]
    lines += ["", "The review stays open: please review again after the push. The bot never approves, dismisses or merges."]
    return "\n".join(lines)


def _gh(env) -> GitHub:
    return GitHub(env["CENTRAL_REPO"], env.get("GITHUB_TOKEN") or env["DOCS_SYNC_PAT"])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ
    number, review_id, base = int(env["PR_NUMBER"]), int(env["REVIEW_ID"]), env.get("TARGET_BRANCH", "qa")
    gh = _gh(env)
    try:
        if "--plan" in argv:
            g = guard(gh, number, review_id, base)
            out = {"proceed": g["proceed"], "reason": g.get("reason", "")}
            if g["proceed"]:
                out.update(repo=g["info"]["repo"], sha=g["info"]["sha"], pages=g["info"]["pages"])
            print(json.dumps(out))
            return 0
        if "--post" in argv:
            with open(RESULT_FILE, encoding="utf-8") as fh:
                result = json.load(fh)
            gh.comment(number, render_comment(result, env.get("HEAD_SHA", "")))
            return 0
        from .. import gitutil as G
        from ..config import repo_policy
        from ..prompts import load_styles
        from .. import writer as W
        from ..wiring import build_deps

        d = build_deps()
        cfg = d["cfg"]
        source_dir = env.get("SOURCE_DIR") or "source-repo"
        g = guard(gh, number, review_id, base, post_cap=False)
        if not g["proceed"]:
            print(json.dumps({"status": "skipped", "reason": g["reason"]}))
            with open(RESULT_FILE, "w", encoding="utf-8") as fh:
                json.dump({"status": "skipped", "reason": g["reason"], "review_id": review_id, "pages": [], "not_acted": [], "changed": []}, fh)
            return 0
        repo = g["info"]["repo"]
        policy = repo_policy(d["reposConfig"], repo)
        d["facts"].migrate()
        instructions = open(cfg["paths"]["instructions"], encoding="utf-8").read() if os.path.exists(cfg["paths"]["instructions"]) else ""

        class Logger:
            def log(self, e: dict) -> None:
                print(f"    [{e['node']}] {e['status']} {e['ms']}ms {json.dumps(e.get('note'), default=str)}")

        deps = {"styles": load_styles(cfg["paths"]["styles"]), "cfg": cfg, "llm": d["llm"], "vectors": d["vectors"], "codeVectors": d["codeVectors"], "facts": d["facts"],
                "registry": d["registry"], "policy": policy, "instructions": instructions, "templateFiles": W.find_template_files(cfg["paths"]["templates"]),
                "defaultTemplate": os.path.join(cfg["paths"]["templates"], "default-template", "default-template.md"), "githubHost": env.get("GIT_HOST"),
                "repoGit": G.accessors(source_dir), "siteRepo": env.get("CENTRAL_REPO")}
        result = run_revision(gh, number=number, review_id=review_id, base_branch=base, deps=deps, acc=G.accessors(source_dir), root=".",
                              commit=G.head(source_dir), logger=Logger())
        with open(RESULT_FILE, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(json.dumps({k: result[k] for k in ("status", "changed", "not_acted")}))
        if result["status"] in ("failed", "noop"):
            gh.comment(number, render_comment(result))  # nothing will be pushed, so say so now
        d["facts"].close()
        return 0
    finally:
        gh.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
