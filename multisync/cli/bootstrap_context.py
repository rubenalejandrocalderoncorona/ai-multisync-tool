"""Load the WHOLE context of a repository into the vector database, before any sync runs.
(A normal sync also self-heals the index, so this is for the first load, rebuilds and rehearsal.)

  python -m multisync.cli.bootstrap_context --repo owner/name --dir /path/to/checkout [--ref <sha>] [--full]
                                            [--site-dir /path/to/documentation-repo --site-repo owner/docs-repo]

Code context  -> code collection  (every in-scope source file, chunked)
Semantic ctx  -> docs collection  (the repo's README/docs and any `docs` globs as kind=source_doc, page briefs as kind=brief;
                                   with --site-dir, the pages of the documentation site as kind=site_doc)
"""
from __future__ import annotations

import os
import sys

from .. import gitutil as G
from ..codesource import pages_scope
from ..config import repo_policy
from ..context import sync_context, sync_site
from ..sitedocs import read_site_file, site_commit, site_files
from ..util import arg_value
from ..wiring import build_deps


def main(argv: list[str]) -> None:
    repo = arg_value(argv, "repo")
    directory = os.path.abspath(arg_value(argv, "dir") or ".")
    if not repo:
        raise SystemExit("usage: bootstrap_context --repo owner/name --dir <checkout> [--ref sha] [--full]")
    d = build_deps()
    commit = arg_value(argv, "ref") or G.head(directory)
    policy = repo_policy(d["reposConfig"], repo)
    d["facts"].migrate()
    d["vectors"].ensure_collection()
    d["codeVectors"].ensure_collection()
    stats = sync_context(repo=repo, commit=commit, before="", full=True, git=G.accessors(directory), code_store=d["codeVectors"], doc_store=d["vectors"],
                         llm=d["llm"], facts=d["facts"], pages=policy.get("pages") or [], scope=pages_scope(policy.get("pages")),
                         exclude=policy.get("exclude") or [], docs=policy.get("docs") or [])
    print(f"bootstrapped {repo}@{commit[:7]}: {stats['repoFiles']} files, {stats['codeChunks']} code chunks, {stats['docChunks']} semantic chunks, {stats['ms']}ms")
    print(f"vector DB now holds {d['codeVectors'].count(repo)} code chunks and {d['vectors'].count(repo)} semantic chunks for {repo}")
    if arg_value(argv, "site-dir") and arg_value(argv, "site-repo"):
        sd = os.path.abspath(arg_value(argv, "site-dir"))
        site = sync_site(site_repo=arg_value(argv, "site-repo"), commit=site_commit(sd), files=site_files(sd), read_file=read_site_file(sd), doc_store=d["vectors"], llm=d["llm"], facts=d["facts"])
        print(f"site docs {arg_value(argv, 'site-repo')}: {site['reason'] if site.get('skipped') else str(site['pages']) + ' pages, ' + str(site['chunks']) + ' chunks'}")
    d["facts"].close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
