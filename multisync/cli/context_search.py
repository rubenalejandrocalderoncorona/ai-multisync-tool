"""See what is in the vector database, and what a query would retrieve from it.

  python -m multisync.cli.context_search --status
  python -m multisync.cli.context_search --repo owner/name --query "how are polls created" [--kind code|semantic|both] [--k 6]
  python -m multisync.cli.context_search --repo owner/name --gar "The service lets participants vote on meeting times."

Code context comes from the code collection; semantic context (approved pages, source docs, briefs, and the rest of the documentation
site) from the docs collection. --gar is the same retrieval the pipeline does with a hypothetical documentation paragraph.
"""
from __future__ import annotations

import os
import re
import sys

from ..context import retrieve_code, retrieve_semantic
from ..util import arg_value, print_table
from ..wiring import build_deps


def search_context(llm, code_store, doc_store, repo, query, kind="both", k=6, site_repo=None) -> dict:
    out = {}
    if kind in ("code", "both"):
        out["code"] = retrieve_code(llm=llm, store=code_store, repo=repo, queries=[query], top_k=k, budget_chars=10**9)
    if kind in ("semantic", "both"):
        out["semantic"] = retrieve_semantic(llm=llm, store=doc_store, repo=repo, queries=[query], top_k=k, site_repo=site_repo)
    return out


def context_status(code_store, doc_store, facts) -> list[dict]:
    rows = []
    for st in facts.list_context_state():
        is_site = st["repo"].startswith("site:")
        rows.append({
            "repo": st["repo"], "commit": str(st["commit"])[:7], "files": st["files"],
            "code": "-" if is_site else code_store.count(st["repo"], "code"),
            "source_doc": "-" if is_site else doc_store.count(st["repo"], "source_doc"),
            "brief": "-" if is_site else doc_store.count(st["repo"], "brief"),
            "approved": "-" if is_site else doc_store.count(st["repo"], "approved"),
            "site_doc": doc_store.count(st["repo"], "site_doc") if is_site else "-",
        })
    return rows


def excerpt(t, n=110) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^FILE .*\n", "", str(t)))[:n]


def main(argv: list[str]) -> None:
    d = build_deps()
    if "--status" in argv:
        print_table(context_status(d["codeVectors"], d["vectors"], d["facts"]))
    else:
        repo = arg_value(argv, "repo")
        query = arg_value(argv, "query") or arg_value(argv, "gar")
        if not repo or not query:
            raise SystemExit('usage: --status | --repo owner/name (--query "..." | --gar "paragraph") [--kind code|semantic|both] [--k 6]')
        r = search_context(d["llm"], d["codeVectors"], d["vectors"], repo, query, arg_value(argv, "kind") or "both", int(arg_value(argv, "k") or 6), os.environ.get("SITE_REPO"))
        for c in r.get("code", []):
            print(f"CODE      {c['score']:.3f}  {c['path']}:{c['start']}-{c['end']}\n          {excerpt(c['text'])}")
        for c in r.get("semantic", []):
            print(f"SEMANTIC  {c['score']:.3f}  [{c['kind']}] {c['path']}{' > ' + c['heading'] if c.get('heading') else ''}\n          {excerpt(c['text'])}")
        if not r.get("code") and not r.get("semantic"):
            print("no results: is this repo loaded? run --status")
    d["facts"].close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
