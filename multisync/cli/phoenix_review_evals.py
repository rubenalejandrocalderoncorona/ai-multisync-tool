"""Export the review outcomes and confidence floors to Phoenix. Informational only: nothing is approved on this basis.

  python -m multisync.cli.phoenix_review_evals [--dry-run] [--min-samples N] [--threshold X] [--json]   (or: multisync phoenix-review-evals ...)

--dry-run reads the database, prints the per-segment table and makes no network call to Phoenix.
env: FACTSTORE_DATABASE_URL (the FactStore), PHOENIX_COLLECTOR_ENDPOINT, PHOENIX_API_KEY, PHOENIX_PROJECT_NAME (default multirepo-agent-docs).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..observability.phoenix_exporter import format_table, sync_review_evals


def main(argv: list[str] | None = None, env=None, facts=None, client=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="phoenix_review_evals", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-samples", type=int, default=int(env.get("REVIEW_READINESS_MIN_SAMPLES", 30)))
    ap.add_argument("--threshold", type=float, default=float(env.get("REVIEW_READINESS_THRESHOLD", 0.85)))
    ap.add_argument("--json", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    close = facts is None
    if facts is None:
        from ..config import load_config
        from ..factstore import create_fact_store

        facts = create_fact_store(load_config(env)["factstore"])
    project = "multirepo-agent-docs"
    try:
        if not a.dry_run:
            from ..evals import phoenix_io as px

            client = client or px.make_client(env)
            project = px.project_name(env)
        res = sync_review_evals(facts, client, min_samples=a.min_samples, threshold=a.threshold, dry_run=a.dry_run, project=project)
    finally:
        if close:
            facts.close()
    print(json.dumps(res, indent=1, default=str) if a.json else format_table(res))
    if not a.dry_run and not a.json:
        print(f"exported {res['review_spans']} review_outcome and {res['segment_spans']} review_segment_metrics spans ({res['duplicates']} already in Phoenix) to project {project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
