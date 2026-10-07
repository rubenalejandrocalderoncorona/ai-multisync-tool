"""Draft evaluations: look at what the nightly evaluator (multisync/evals/run_draft_evals.py) logged. Read-only.

  multisync evals flag-low-scores [--threshold 0.7] [--hours 24] [--ticket]

Lists the drafts scored below the threshold (the score is the share of supported claims, 0..1) with their change unit, repo, unsupported quotes and PR link (the source commit while no review PR is recorded), for a human to
follow up. Exits 0. Only with --ticket does it open a cAImanDesk ticket (one per change unit, no duplicates), through multisync/tickets.py.
env: PHOENIX_COLLECTOR_ENDPOINT, PHOENIX_API_KEY, PHOENIX_PROJECT_NAME; FACTSTORE_DATABASE_URL (optional: finds the review PR link); CAIMANDESK_* for --ticket.
"""
from __future__ import annotations

import argparse
import os
import sys

from ..config import load_config
from ..evals.draft_evals import DEFAULT_THRESHOLD, annotation_records, flag_low, format_findings, span_info
from ..factstore import create_fact_store
from ..tickets import create_tickets


def run(argv: list[str], env=None, client=None, facts=None, tickets=None) -> tuple[int, str]:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="multisync evals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fl = sub.add_parser("flag-low-scores")
    fl.add_argument("--threshold", type=float, default=float(env.get("DRAFT_EVALS_THRESHOLD") or DEFAULT_THRESHOLD), help="default 0.7 (the unfaithful band), or DRAFT_EVALS_THRESHOLD")
    fl.add_argument("--hours", type=float, default=24, help="how far back to look for draft spans")
    fl.add_argument("--ticket", action="store_true", help="also open a cAImanDesk ticket per flagged change unit (never without this flag)")
    a = ap.parse_args(argv)

    from ..evals import phoenix_io as px

    client = client or px.make_client(env)
    spans = px.draft_span_rows(client, px.project_name(env), a.hours, 5000)
    by_id = {r["context.span_id"]: r for r in spans if r.get("context.span_id")}
    if not by_id:
        return 0, f"no draft spans in the last {a.hours:g}h"
    scores = annotation_records(px.logged_scores(client, px.project_name(env), list(by_id)))
    cfg = load_config(env)
    facts = facts if facts is not None else (create_fact_store(cfg["factstore"]) if cfg["factstore"]["driver"] == "postgres" else None)
    pr_urls = {}
    try:
        if facts is not None:
            pr_urls = facts.pr_urls(sorted({i["change_unit_id"] for i in map(span_info, spans) if i["change_unit_id"]}))
    except Exception as e:  # noqa: BLE001 - the link falls back to the commit
        print(f"review PR links not looked up: {e}", file=sys.stderr)
    flagged = flag_low(scores, spans, a.threshold, pr_urls)
    text = format_findings(flagged, a.threshold, len(scores))
    if a.ticket and flagged:
        tickets = tickets or create_tickets(cfg["alerts"])
        if not tickets.enabled:
            text += "\nno ticket opened: ticketing is not configured"
        else:
            worst = {}
            for f in flagged:  # worst first: one ticket per change unit, for its lowest-scoring attempt
                worst.setdefault(f["change_unit_id"], f)
            for f in worst.values():
                try:
                    text += f"\nticket: {tickets.open_eval_flag(f, a.threshold)}"
                except Exception as e:  # noqa: BLE001 - a ticket problem never fails the listing
                    text += f"\nticket for {f['change_unit_id']} not opened: {e}"
    return 0, text


def main(argv: list[str] | None = None) -> int:
    try:
        code, text = run(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    except ImportError as e:
        print(f"this command needs the evals extra (pip install -r requirements-evals.txt): {e}", file=sys.stderr)
        return 1
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
