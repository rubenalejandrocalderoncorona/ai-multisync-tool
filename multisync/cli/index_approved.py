"""Index human-approved documentation into the vector store.

Runs when a docs-sync PR is merged. This is the ONLY path, besides auto-trust publishing, that writes to the index: drafts and GAR
text never do.

Usage: python -m multisync.cli.index_approved <file.md> [more.md ...]
       python -m multisync.cli.index_approved --delete <removed-file.md> ...   (drop pages removed by the PR)
  Provenance (repo, source path, commit) comes from the file's front matter.
"""
from __future__ import annotations

import os
import re
import sys

from ..context import record_doc_refs
from ..vectorstore import index_approved
from ..wiring import build_deps


def provenance(content: str) -> dict | None:
    fm_m = re.match(r"^---\n([\s\S]*?)\n---", content)
    fm = fm_m.group(1) if fm_m else ""
    source = (re.search(r"(?m)^source:\s*(\S+)", fm) or [None, ""])[1]
    commit = (re.search(r"(?m)^commit:\s*(\S+)", fm) or [None, ""])[1]
    dk = re.search(r"(?m)^doc_key:\s*(.+)$", fm)
    doc_key = dk.group(1).strip() if dk else None
    m = re.match(r"^https?://[^/]+/([^/]+/[^/]+)/(?:blob/[^/]+/(.+)|tree/[^/]+/?)$", source)
    if not m:
        return None
    file_path = doc_key or m.group(2)
    return {"repo": m.group(1), "file_path": file_path, "commit": commit} if file_path else None


def main(args: list[str]) -> None:
    delete = bool(args) and args[0] == "--delete"
    files = args[1:] if delete else args
    if not files:
        print("no files to index")
        return
    d = build_deps()
    d["facts"].migrate()
    d["vectors"].ensure_collection()
    for f in files:
        if not os.path.exists(f):
            continue
        content = open(f, encoding="utf-8").read()
        p = provenance(content)
        if not p:
            print(f"skip {f}: no provenance front matter (not an auto-synced page)", file=sys.stderr)
            continue
        if delete:
            d["vectors"].delete_by_path(p["repo"], p["file_path"])
            print(f"removed {p['repo']}/{p['file_path']} from the index")
            continue
        n = index_approved(d["vectors"], d["llm"], p["repo"], p["file_path"], content, p["commit"])
        d["facts"].approve_claims(p["repo"], p["file_path"], p["commit"])
        if hasattr(d["facts"], "known_symbol_names"):
            record_doc_refs(d["facts"], p["repo"], p["file_path"], content, "approved", d["facts"].known_symbol_names())
        print(f"indexed {f}: {n} chunk(s) @ {p['commit'][:7]}")
    d["facts"].close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
