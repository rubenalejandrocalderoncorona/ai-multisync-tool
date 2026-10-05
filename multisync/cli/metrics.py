"""Read-only metrics over the review outcomes.

  multisync metrics review-readiness --segment <diff_classification>:<model_tier> [--threshold 0.85] [--min-samples 30] [--confidence 1.96]
                                     [--policy-version v1-xxxxxxxx] [--json]

Segment: internal|public_interface : cheap|expensive, for example public_interface:expensive.
Reports the Wilson score lower bound of the share of drafts a reviewer merged unchanged (draft_with_noedition) and whether it clears the threshold at
the minimum sample size. Exit code 0 = ready, 1 = not ready, 2 = usage error. It decides nothing: no approval path reads it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..review_outcomes import SEGMENT_CLASSES, SEGMENT_TIERS, readiness
from ..wiring import build_deps


def parse_segment(text: str) -> tuple[str, str]:
    cls, _, tier = text.partition(":")
    if cls not in SEGMENT_CLASSES or tier not in SEGMENT_TIERS:
        raise argparse.ArgumentTypeError(f"segment must be <{'|'.join(SEGMENT_CLASSES)}>:<{'|'.join(SEGMENT_TIERS)}>, got {text!r}")
    return cls, tier


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="multisync metrics", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review-readiness", help="Wilson lower bound of the no-edit rate for one segment, against a threshold")
    r.add_argument("--segment", required=True, type=parse_segment, metavar="CLASS:TIER")
    r.add_argument("--threshold", type=float, default=float(os.environ.get("REVIEW_READINESS_THRESHOLD", 0.85)))
    r.add_argument("--min-samples", type=int, default=int(os.environ.get("REVIEW_READINESS_MIN_SAMPLES", 30)))
    r.add_argument("--confidence", type=float, default=1.96, help="z value of the interval (1.96 = 95%%)")
    r.add_argument("--policy-version", default=None, help="only count outcomes produced under this policy version")
    r.add_argument("--json", action="store_true")
    return ap


def run(args, facts) -> tuple[int, str]:
    cls, tier = args.segment
    res = readiness(facts.review_outcome_counts(cls, tier, args.policy_version), args.threshold, args.min_samples, args.confidence)
    if args.json:
        return (0 if res["ready"] else 1), json.dumps({"segment": f"{cls}:{tier}", "policy_version": args.policy_version, **res}, indent=1)
    lines = [
        f"segment {cls}:{tier}" + (f"  policy {args.policy_version}" if args.policy_version else "  (all policy versions)"),
        f"  reviewed drafts       {res['n']}  (unchanged {res['noedition']}, edited {res['edition']}, rejected {res['rejected']})",
        f"  no-edit rate          {res['rate']:.3f}",
        f"  Wilson interval       [{res['wilson_lower']:.3f}, {res['wilson_upper']:.3f}]  (z = {res['z']})",
        f"  threshold / min n     lower bound >= {res['threshold']}  and  n >= {res['min_samples']}",
        f"  verdict               {'READY' if res['ready'] else 'NOT READY'}" + ("" if res["ready"] else ": " + "; ".join(res["reasons"])),
        "  (read-only: nothing is auto-approved)",
    ]
    return (0 if res["ready"] else 1), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    d = build_deps()
    try:
        code, text = run(args, d["facts"])
    finally:
        d["facts"].close()
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
