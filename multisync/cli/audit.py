"""The second-pass audit queue. A share of the drafts a reviewer merged unchanged are sampled (AUDIT_SAMPLE_RATE, default 0.10) for an independent check.

  multisync audit list                                                       the open audits (no reviewer, outcome or scores: the auditor must not be anchored)
  multisync audit show <id>                                                  what to check: the page and the code commit to check it against
  multisync audit submit <id> --reviewer <login> --accurate yes|no [--notes "..."]

The auditor confirms the page's factual statements against the code at the commit, independently of the first review. The original reviewer cannot audit
their own review: the submission is refused. Detection only: an audit changes nothing about the page and approves nothing.
"""
from __future__ import annotations

import argparse
import sys

from ..segment_alerts import Notifier, check_drift
from ..tickets import create_tickets
from ..wiring import build_deps


def split(change_unit_id: str) -> tuple[str, str, str]:
    repo, _, rest = change_unit_id.partition("@")
    commit, _, page = rest.partition(":")
    return repo, commit, page


def run(argv: list[str], facts, notifier: Notifier | None = None) -> tuple[int, str]:
    ap = argparse.ArgumentParser(prog="multisync audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("id", type=int)
    sm = sub.add_parser("submit")
    sm.add_argument("id", type=int)
    sm.add_argument("--reviewer", required=True)
    sm.add_argument("--accurate", required=True, choices=["yes", "no"])
    sm.add_argument("--notes", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "list":
        rows = facts.pending_audits()
        if not rows:
            return 0, "no open audits"
        return 0, "\n".join(f"#{r['id']}  {split(r['change_unit_id'])[2]}  ({r['repo']} @ {split(r['change_unit_id'])[1][:7]})" for r in rows)
    if a.cmd == "show":
        item = facts.audit_item(a.id)
        if not item or not item["audit_sampled"]:
            return 1, f"audit {a.id}: not found or not sampled for audit"
        repo, commit, page = split(item["change_unit_id"])
        done = " (already audited)" if item["audit_verified_accurate"] is not None else ""
        return 0, (f"audit #{item['id']}{done}\n  page      {page}\n  source    https://github.com/{repo}/tree/{commit}\n"
                   "  task      read the published page and confirm each factual statement against the code at that commit;\n"
                   "            judge it yourself, not by anyone else's review.\n"
                   f"  verdict   multisync audit submit {item['id']} --reviewer <your login> --accurate yes|no --notes \"...\"")
    try:
        facts.submit_audit(a.id, a.reviewer, a.accurate == "yes", a.notes)
    except ValueError as e:
        return 1, f"refused: {e}"
    item = facts.audit_item(a.id)  # a verdict may tip the segment's audited accuracy below the alert level: tell a human once (alerting only)
    if item:
        check_drift(facts, item["diff_classification"], item["model_tier_used"], item["policy_version"], notifier or Notifier(None))
    return 0, f"audit #{a.id} recorded: {'accurate' if a.accurate == 'yes' else 'NOT accurate'}"


def main(argv: list[str] | None = None) -> int:
    d = build_deps()
    try:
        code, text = run(argv if argv is not None else sys.argv[1:], d["facts"], Notifier(d["cfg"]["alerts"], create_tickets(d["cfg"]["alerts"])))
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    finally:
        d["facts"].close()
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
