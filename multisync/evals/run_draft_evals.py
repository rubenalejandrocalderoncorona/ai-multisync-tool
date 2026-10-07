"""Score the drafts of the last day for hallucination and attach the scores to their Phoenix spans. Run nightly by infra/k8s/draft-evals-cronjob.yaml.

  python -m multisync.evals.run_draft_evals [--hours 24] [--max-spans 500] [--limit 5000] [--dry-run]

env: PHOENIX_COLLECTOR_ENDPOINT, PHOENIX_API_KEY, PHOENIX_PROJECT_NAME (default multirepo-agent-docs), and the model keys the pipeline uses (DEEPSEEK_API_KEY, AI_API_KEY).
Reads spans from Phoenix and writes evaluations back; it never touches the generation pipeline, the FactStore or a repo. The judge is the cheap-tier model.
Idempotent: a re-run re-scores the same spans and overwrites the same `draft_faithfulness` annotation on each. No draft spans in the window: prints that and exits 0.
"""
from __future__ import annotations

import argparse
import os
import sys

from ..config import load_config
from ..llm import LLM
from .draft_evals import EVAL_NAME, annotations_from_scores, build_inputs
from .judge import PipelineJudge


def run(argv: list[str], env=None, client=None, evaluate=None, log=None, judge=None) -> int:
    """`client`, `evaluate`, `log` and `judge` are for tests; the real ones are built lazily."""
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="run_draft_evals")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max-spans", type=int, default=500, help="cost ceiling: at most this many drafts go to the judge")
    ap.add_argument("--limit", type=int, default=5000, help="most spans to read from Phoenix")
    ap.add_argument("--dry-run", action="store_true", help="read and build the evaluator inputs, call no model, log nothing")
    a = ap.parse_args(argv)

    from . import phoenix_io as px

    client = client or px.make_client(env)
    rows = px.draft_span_rows(client, px.project_name(env), a.hours, a.limit)
    if not rows:
        print(f"no draft spans in project {px.project_name(env)} in the last {a.hours:g}h; nothing to evaluate")
        return 0
    inputs, skipped = build_inputs(rows)
    for why in skipped:
        print(f"skipped {why}", file=sys.stderr)
    if len(inputs) > a.max_spans:
        print(f"{len(inputs)} drafts found, evaluating the newest-listed {a.max_spans} (--max-spans)", file=sys.stderr)
        inputs = inputs[: a.max_spans]
    print(f"{len(rows)} draft spans, {len(inputs)} evaluable, {len(skipped)} skipped")
    if a.dry_run or not inputs:
        return 0

    judge = judge or PipelineJudge(LLM(load_config(env)["ai"]))
    scored, column = (evaluate or px.evaluate)(inputs, judge)
    annotations, failed = annotations_from_scores(scored, column)
    for sid in failed:
        print(f"judge gave no score for span {sid}", file=sys.stderr)
    n = (log or px.log_evaluations)(client, annotations) if annotations else 0
    low = sum(1 for x in annotations if x["score"] < 1.0)
    print(f"logged {n} {EVAL_NAME} evaluations ({low} below 1.0, {len(failed)} unscored) with judge {judge.model}")
    return 0 if annotations else 1  # every judge call failed (a bad key, an outage): let the Job show it


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except ImportError as e:
        print(f"the evaluator needs the evals extra (pip install -r requirements-evals.txt): {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
