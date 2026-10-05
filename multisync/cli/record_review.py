"""Record the outcome of a closed docs-sync pull request in review_outcomes. LOGGING ONLY. Run by the Job the receiver starts when a review PR is
closed (merged: after indexing; closed unmerged: on its own). It never fails the Job: a problem is printed and the exit code stays 0.

env: CENTRAL_REPO  PR_NUMBER  PR_URL  PR_MERGED (true|false)  REVIEWED_BY  REVIEWED_AT  GITHUB_TOKEN (or DOCS_SYNC_PAT)  + FACTSTORE_DATABASE_URL
"""
from __future__ import annotations

import json
import os
import sys

from ..review_outcomes import GitHub, record
from ..tickets import create_tickets
from ..wiring import build_deps


def main() -> int:
    env = os.environ
    try:
        number = int(env["PR_NUMBER"])
        d = build_deps()
        gh = GitHub(env["CENTRAL_REPO"], env.get("GITHUB_TOKEN") or env["DOCS_SYNC_PAT"])
        d["facts"].migrate()
        tickets = create_tickets(d["cfg"]["alerts"])

        def open_ticket(audit_id, row):  # the audit queue is the table; the ticket is how the second reviewer hears about it
            repo, _, rest = row["change_unit_id"].partition("@")
            commit, _, page = rest.partition(":")
            url = tickets.open_audit(audit_id, repo, commit, page)
            if url:
                print(f"audit {audit_id} requested: {url}")

        result = record(d["facts"], gh, number, env["PR_URL"], env.get("REVIEWED_BY") or None, env.get("REVIEWED_AT") or None, env.get("PR_MERGED") == "true", on_sampled=open_ticket)
        print(json.dumps(result))
        d["facts"].close()
    except Exception as e:  # noqa: BLE001 - logging must never fail the job that carries it
        print(f"review outcome not recorded: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
