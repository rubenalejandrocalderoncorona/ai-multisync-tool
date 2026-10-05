"""The FactStore: claims, decisions, the per-stage audit log, and the code -> docs coupling the model router reads.

  python -m multisync.cli.factstore --migrate                          create or update the schema (idempotent)
  python -m multisync.cli.factstore --status                           row counts and the coupling per repo
  python -m multisync.cli.factstore --symbol createPoll                where a symbol is defined and which documents mention it
  python -m multisync.cli.factstore --backfill --repo owner/name --dir <checkout> [--site-dir <docs repo> --site-repo owner/docs]
                                                                       fill symbols and doc references for a repo that is already loaded
                                                                       (no embeddings, no cost)
"""
from __future__ import annotations

import sys

from .. import gitutil as G
from ..codesource import pages_scope
from ..config import load_config, read_json, repo_policy
from ..context import backfill_coupling
from ..sitedocs import read_site_file, site_files
from ..util import arg_value, attrs, print_table
from ..wiring import build_deps


def main(argv: list[str]) -> None:
    d = build_deps()
    f = d["facts"]
    f.migrate()
    if "--migrate" in argv:
        print("FactStore schema is up to date.")

    if "--status" in argv:
        if hasattr(f, "conn"):
            counts = {}
            for t in ("claims", "decisions", "node_logs", "context_state", "symbols", "doc_refs"):
                counts[t] = f._q(f"SELECT count(*)::int n FROM {t}")[0]["n"]
            print_table([counts])
        print("Public symbols per repo:")
        print_table(f.coupling_stats())

    sym = arg_value(argv, "symbol")
    if sym:
        info = f.symbol_info(sym)
        print(f"\nDefined in ({len(info['defined'])}):")
        for x in info["defined"][:15]:
            print(f"  {x['repo']}  {x['path']}  [{x['kind']}]")
        print(f"Mentioned by ({len(info['referencedBy'])}):")
        for x in info["referencedBy"][:15]:
            print(f"  {x['doc_repo']}  {x['doc_path']}  [{x['kind']}]")
        if not info["defined"] and not info["referencedBy"]:
            print("  (unknown symbol: is the repo loaded? try --backfill)")

    if "--backfill" in argv:
        repo, directory = arg_value(argv, "repo"), arg_value(argv, "dir")
        if not repo or not directory:
            raise SystemExit("usage: --backfill --repo owner/name --dir <checkout> [--site-dir ... --site-repo ...]")
        cfg = load_config()
        policy = repo_policy(attrs(read_json(cfg["paths"]["reposConfig"], {"defaults": {}, "repos": {}})), repo)
        site = None
        site_repo = arg_value(argv, "site-repo")
        if arg_value(argv, "site-dir") and site_repo:
            sd = arg_value(argv, "site-dir")
            read = read_site_file(sd)
            site = {p: t for p in site_files(sd) if (t := read(p)) is not None}
        r = backfill_coupling(repo=repo, commit=arg_value(argv, "ref") or G.head(directory), git=G.accessors(directory), facts=f, scope=pages_scope(policy.get("pages")),
                              exclude=policy.get("exclude") or [], docs=policy.get("docs") or [], site_files=site, site_repo=site_repo)
        extra = f", {r['siteRefs']} in the docs site" if site else ""
        print(f"backfilled {repo}: {r['symbols']} public symbols ({', '.join(r['kinds'])}) from {r['files']} files; {r['docRefs']} doc references in {r['docFiles']} docs{extra}")
    f.close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
