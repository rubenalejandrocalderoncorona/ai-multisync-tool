"""The central place to read what the pipeline did, from the FactStore (Postgres), not from pod logs that vanish after five minutes.

  python -m multisync.cli.runs                      the most recent runs: repo, when, stages, errors, cost, outcomes
  python -m multisync.cli.runs --repo owner/name    only that repository
  python -m multisync.cli.runs <run_id>             every stage of one run, with the decision data of each node
  python -m multisync.cli.runs <run_id> --json      the same as JSON

Every node of the LangGraph graph writes one row (node_logs) as it finishes, so a run that fails halfway is still visible here.
For the same runs as trace trees (every prompt and answer) open Phoenix at /phoenix, see docs/OBSERVABILITY.md.
"""
from __future__ import annotations

import json
import sys

from ..util import arg_value, print_table
from ..wiring import build_deps


def short(note: dict, width: int = 150) -> str:
    keep = {k: v for k, v in (note or {}).items() if k not in ("relatedTop", "usage", "prompt", "reasons", "relatedFiles")}
    usage = (note or {}).get("usage") or {}
    usd = sum((usage.get(t) or {}).get("usd", 0) for t in ("cheap", "expensive"))
    text = json.dumps(keep, default=str)
    return (text[:width] + "...") if len(text) > width else text + (f"  ${usd:.4f}" if usd else "")


def main(argv: list[str]) -> None:
    d = build_deps()
    f = d["facts"]
    run_id = next((a for a in argv if not a.startswith("--") and a not in (arg_value(argv, "repo"), arg_value(argv, "limit"))), None)
    if run_id:
        rows = f.run_logs(run_id)
        if "--json" in argv:
            print(json.dumps(rows, default=str, indent=1))
        else:
            if not rows:
                print("no such run")
            for r in rows:
                print(f"{str(r['created_at'])[:19]}  {r['node'].ljust(16)} {r['status'].ljust(9)} {str(r['ms']).rjust(7)}ms  {r['path']}")
                print(f"      {short(r['note'])}")
    else:
        runs = f.list_runs(int(arg_value(argv, "limit") or 20), arg_value(argv, "repo"))
        print_table([{"run": r["run_id"], "repo": r["repo"], "started": str(r["started"])[:19], "stages": r["nodes"], "errors": r["errors"],
                      "usd": f"{r['usd']:.3f}", "outcomes": ", ".join(f"{k}:{v}" for k, v in r["outcomes"].items()) or "-"} for r in runs])
        print("\nOne run in detail: python -m multisync.cli.runs <run_id>  (the id in the first column)")
    f.close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)
