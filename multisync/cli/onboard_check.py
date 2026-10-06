"""Dry run for onboarding a repository. NO network calls, nothing is written: it shows what the repo's entry in config/repos.json would
load into the vector database and what each page would be based on, so a wrong scope, a sensitive file or a surprising cost is caught
BEFORE anything is embedded or sent to a model.

  python -m multisync.cli.onboard_check --repo owner/name --dir /path/to/checkout [--ref <sha>]

Exit code 1 when there are blocking problems (no config entry, a page that matches no files, sensitive files in scope).
"""
from __future__ import annotations

import json
import re
import sys

from .. import gitutil as G
from ..chunker import chunk_code, chunk_markdown
from ..codesource import matches_any, pages_scope, scrub, select_files, snapshot
from ..config import load_config, read_json, repo_policy
from ..context import DOC_FILE
from ..coverage import check_coverage
from ..prompts import load_styles, resolve_style
from ..util import arg_value, attrs, print_table

# Classification markers in document-type files (notes, exports, fixtures). Ordinary code that uses the word "restricted" is not flagged.
SENSITIVE_CONTENT = re.compile(r"(classification\s*:[^\n]*\b(restricted|confidential|secret)\b|internal use only|do not distribute|strictly confidential|confidential and proprietary)", re.I)
DOCLIKE = re.compile(r"\.(md|mdx|txt|csv|json|ya?ml)$", re.I)
# Secret-type FILES and folders, not ordinary code that merely mentions credentials (features/credentials/data.ts is fine).
SENSITIVE_PATH = re.compile(r"(^|/)(\.env(\..+)?$|[^/]*\.(pem|key|p12|pfx|keystore)$|id_(rsa|ed25519|ecdsa)$|credentials\.(json|ya?ml)$|secrets?\.(json|ya?ml)$|secrets?/)", re.I)
EMBED_USD_PER_MTOK = 0.02  # text-embedding-3-small


def plan_onboarding(repo: str, policy, git, commit: str, styles: dict | None = None) -> dict:
    """git: object with list_files(rev) and read_at(rev, file)."""
    styles = styles or {"styles": {}}
    warnings: list[str] = []
    blockers: list[str] = []
    all_files = git.list_files(commit)
    exclude = policy.get("exclude") or []
    scope = pages_scope(policy.get("pages"))
    allowed = select_files(all_files, scope, exclude)
    allowed_set = set(allowed)
    code_files = [f for f in allowed if not DOC_FILE.match(f)]
    doc_files = [f for f in all_files if (DOC_FILE.match(f) or matches_any(f, policy.get("docs") or [])) and not matches_any(f, exclude)]

    code_chars = code_chunks = skipped = 0
    secret_files: list[str] = []
    sensitive = {f for f in all_files if SENSITIVE_PATH.search(f) and f in allowed_set}
    for f in code_files:
        raw = git.read_at(commit, f)
        if raw is None or "\u0000" in raw or len(raw.encode("utf-8")) > 200_000:
            skipped += 1
            continue
        clean = scrub(raw)
        if clean != raw:
            secret_files.append(f)
        if DOCLIKE.search(f) and SENSITIVE_CONTENT.search(raw):
            sensitive.add(f)
        code_chars += len(clean)
        code_chunks += len(chunk_code(f, clean))
    doc_chars = doc_chunks = 0
    for f in doc_files:
        raw = git.read_at(commit, f)
        if raw is None:
            continue
        if SENSITIVE_CONTENT.search(raw):
            sensitive.add(f)
        doc_chars += len(raw)
        doc_chunks += len(chunk_markdown(raw))

    pages = []
    for page in policy.get("pages") or [{"path": "overview.md", "kind": policy.get("style")}]:
        files = select_files(all_files, page.get("scope"), [*(page.get("exclude") or []), *exclude])
        snap = snapshot(lambda f: git.read_at(commit, f), files, changed=files)
        style = resolve_style(styles, page.get("kind") or policy.get("style"))
        row = {"path": page["path"], "style": style["key"], "styleFallback": style.get("fallback"), "scopeFiles": len(files), "snapshotFiles": len(snap["files"]),
               "snapshotChars": len(snap["text"]), "truncated": len(snap["files"]) < len(files), "hasBrief": bool(page.get("brief")), "coverage": None}
        if style.get("coverage"):
            c = check_coverage("", snap["text"], style["coverage"])
            row["coverage"] = {k: v["total"] for k, v in c["kinds"].items()} if c else {}
        if not files:
            blockers.append(f"page {page['path']}: its scope matches NO files (scope: {', '.join(page.get('scope') or ['**'])})")
        elif row["truncated"]:
            warnings.append(f"page {page['path']}: {len(files) - len(snap['files'])} of {len(files)} in-scope files do not fit the model's snapshot (they are still reachable through retrieval). Narrow the scope or split the page.")
        if row["styleFallback"]:
            warnings.append(f"page {page['path']}: style \"{page.get('kind')}\" is not in config/doc-styles.json; the default style would be used")
        if not row["hasBrief"]:
            warnings.append(f'page {page["path"]}: no "brief". A brief tells the planner what the page is for and is searched as semantic context')
        if style.get("coverage") and row["coverage"] and all(n == 0 for n in row["coverage"].values()):
            warnings.append(f"page {page['path']}: style \"{style['key']}\" enforces coverage but no declared names were found in scope (is the scope right?)")
        pages.append(row)

    # The owner can acknowledge a file that only mentions a sensitive word (an example in a README) with `allowSensitive: ["path", ...]` in the repo's config.
    sensitive -= set(policy.get("allowSensitive") or [])
    if len({p["path"] for p in pages}) != len(pages):
        blockers.append("two pages share the same path")
    if sensitive:
        listed = ", ".join(sorted(sensitive)[:6]) + (", ..." if len(sensitive) > 6 else "")
        blockers.append(f"{len(sensitive)} in-scope file(s) look sensitive (a secret-type path, or a \"restricted/confidential\" marker). Exclude them or confirm they may be sent to the model: {listed}")
    if secret_files:
        warnings.append(f"{len(secret_files)} file(s) contain lines that look like secrets; those lines are removed before anything is embedded ({', '.join(secret_files[:4])}{', ...' if len(secret_files) > 4 else ''})")
    if not (policy.get("docs") or []) and not doc_files:
        warnings.append("no existing documentation found (no README or docs/ and no `docs` globs): the semantic context will only hold the page briefs")
    if policy.get("trust") == "auto":
        warnings.append('trust is "auto": pages publish without a human. Start with "review".')
    if not policy.get("glossary"):
        warnings.append("no glossary: set the product name and any upstream names that must not appear")

    tokens = round((code_chars + doc_chars) / 4)
    return {
        "repo": repo, "commit": commit, "mode": policy.get("mode") or "docs", "trust": policy.get("trust"),
        "filesInRepo": len(all_files), "excludedOrOutOfScope": len(all_files) - len(allowed) - len([f for f in doc_files if f not in allowed_set]),
        "code": {"files": len(code_files) - skipped, "skipped": skipped, "chunks": code_chunks, "chars": code_chars},
        "semantic": {"files": len(doc_files), "chunks": doc_chunks + sum(1 for p in pages if p["hasBrief"])},
        "embeddings": {"tokens": tokens, "usd": round(tokens / 1e6 * EMBED_USD_PER_MTOK, 4)},
        "pages": pages, "warnings": warnings, "blockers": blockers,
    }


def print_plan(r: dict) -> None:
    n = lambda x: f"{x:,}"  # noqa: E731
    print(f"\nOnboarding preview: {r['repo']} @ {str(r['commit'])[:7]}   mode={r['mode']}  trust={r['trust']}\n")
    print(f"  repo files               {n(r['filesInRepo'])}")
    skipped = f" ({r['code']['skipped']} skipped: binary or over 200 KB)" if r["code"]["skipped"] else ""
    print(f"  -> code context          {n(r['code']['files'])} files, {n(r['code']['chunks'])} chunks{skipped}")
    print(f"  -> semantic context      {n(r['semantic']['files'])} doc files, {n(r['semantic']['chunks'])} chunks (incl. page briefs)")
    print(f"  -> not indexed           {n(r['excludedOrOutOfScope'])} files (out of scope, excluded, tests, lockfiles, generated)")
    print(f"  embedding estimate       ~{n(r['embeddings']['tokens'])} tokens, about ${r['embeddings']['usd']} (text-embedding-3-small)\n")
    print_table([{"page": p["path"], "style": p["style"], "files in scope": p["scopeFiles"], "sent to model": p["snapshotFiles"], "chars": p["snapshotChars"],
                  "brief": "yes" if p["hasBrief"] else "NO", "must cover": json.dumps(p["coverage"]) if p["coverage"] else "-"} for p in r["pages"]])
    for w in r["warnings"]:
        print(f"  ! {w}")
    for b in r["blockers"]:
        print(f"  ✘ {b}")
    print("\nBlocked: fix the items marked ✘ before onboarding." if r["blockers"] else
          "\nNo blockers. Next: run the Bootstrap Context workflow (or python -m multisync.cli.bootstrap_context), then check with python -m multisync.cli.context_search --status.")


def main(argv: list[str]) -> int:
    repo, directory = arg_value(argv, "repo"), arg_value(argv, "dir")
    if not repo or not directory:
        print("usage: onboard_check --repo owner/name --dir <checkout> [--ref sha]", file=sys.stderr)
        return 2
    cfg = load_config()
    repos_config = attrs(read_json(cfg["paths"]["reposConfig"], {"defaults": {}, "repos": {}}))
    if repo not in (repos_config.get("repos") or {}):
        print(f"✘ {repo} has no entry in {cfg['paths']['reposConfig']}. Add one first (see examples/calendarscheduler/repos.entry.json).", file=sys.stderr)
        return 1
    result = plan_onboarding(repo, repo_policy(repos_config, repo), G.accessors(directory), arg_value(argv, "ref") or G.head(directory), load_styles(cfg["paths"]["styles"]))
    print_plan(result)
    return 1 if result["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
