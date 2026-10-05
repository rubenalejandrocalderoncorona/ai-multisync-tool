"""Review-ticket lifecycle for a docs PR waiting in QA. Never fails a workflow because a ticket could not be made.

  open     after the PR is created: create (or update) the ticket, print JSON { ref, url, created, marker }
           env: PR_URL, RESULTS_FILE (default pipeline-results.json), ENVIRONMENT (default QA)
  qa       the docs PR was merged into the QA branch:   env: PR_URL, PR_BODY, QA_URL (ticket noted, stays open)
  approve  the promotion PR was merged to production:   env: PR_URL, PR_BODY (every ticket in the body is closed)
  reject   a PR was closed without merging:             env: PR_URL, PR_BODY (noted, stays open)

The ticket is found again through a marker the workflow stores in the PR body: <!-- multisync:ticket=desk:42 -->
"""
from __future__ import annotations

import json
import os
import sys

from ..config import load_config
from ..tickets import create_tickets, ticket_marker, ticket_refs_from_body


def run(action: str, env=None, transport=None) -> dict:
    env = os.environ if env is None else env
    cfg = load_config(env)
    tickets = create_tickets(cfg["alerts"], transport)
    pr_url = env.get("PR_URL") or ""
    if not tickets.enabled:
        print("review ticket skipped: ticketing is not configured", file=sys.stderr)
        return {"skipped": True}

    if action == "open":
        with open(env.get("RESULTS_FILE") or "pipeline-results.json", encoding="utf-8") as fh:
            results = json.load(fh)
        items = [{"path": r["path"], **((r.get("metrics") or {}).get("final") or {})} for r in results["results"] if r["outcome"] == "pending_review"]
        t = tickets.open_review(results["repo"], results["commit"], pr_url, items, env.get("ENVIRONMENT") or "QA")
        return {**t, "marker": ticket_marker(t)}

    refs = ticket_refs_from_body(env.get("PR_BODY"))
    if not refs:
        print("no ticket marker in the PR body; nothing to update", file=sys.stderr)
        return {"skipped": True}
    verbs = {
        "qa": lambda r: tickets.qa_deployed(r, pr_url, env.get("QA_URL")),
        "approve": lambda r: tickets.approve(r, pr_url),
        "reject": lambda r: tickets.reject(r, pr_url),
    }
    if action not in verbs:
        raise ValueError(f"unknown action: {action}")
    done = []
    for r in refs:
        try:
            ok = verbs[action](r)
        except Exception as e:  # noqa: BLE001
            print(f"ticket {r['id']}: {e}", file=sys.stderr)
            ok = False
        done.append({"id": r["id"], "ok": ok})
    return {"ok": all(x["ok"] for x in done), "tickets": done}


def main(argv: list[str]) -> None:
    try:
        print(json.dumps(run(argv[0] if argv else "")))
    except Exception as e:  # noqa: BLE001
        print(f"ticket step failed (non-fatal): {e}", file=sys.stderr)
        print(json.dumps({"skipped": True, "error": str(e)}))


if __name__ == "__main__":
    main(sys.argv[1:])
