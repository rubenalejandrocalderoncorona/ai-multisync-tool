"""Show the human review outcomes and the statistical confidence floors in Arize Phoenix, next to the generation and judge traces.

READ-ONLY on `multisync.review_outcomes` (own SELECTs, nothing written, no schema change). INFORMATIONAL ONLY: `ready` here is the same rule as
review_outcomes.readiness; nothing auto-approves and `auto_approval_eligible` is never read.

Per segment (policy_version, diff_classification, model_tier_used): n, no-edit rate, Wilson lower bound, graduation progress, ready, and the
point-biserial correlation (Pearson against the 0/1 'unedited' outcome, rejected rows excluded) between each judge score and the human outcome.

Phoenix gets (project PHOENIX_PROJECT_NAME, default multirepo-agent-docs):
  review_outcome          one span per review row; session.id = the segment key; trace/span ids derived from (pr_url, change_unit_id)
  review_segment_metrics  one span per (segment, policy_version, UTC date) with the numbers as attributes, plus an annotation of the same name on it
Idempotency: ids are deterministic. Phoenix drops a span whose id it already has (the response lists duplicates), so a re-run adds nothing;
the segment numbers change during the day, so they are ALSO written as an annotation, which Phoenix upserts on (name, span), so the latest wins.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from ..review_outcomes import wilson_interval

SPAN_REVIEW = "review_outcome"
SPAN_SEGMENT = "review_segment_metrics"
SCORES = ("precision", "recall", "style", "quality")
MIN_CORR_N = 5
COLUMNS = ("change_unit_id, repo, diff_classification, model_tier_used, similarity_score, judge_score_precision, judge_score_recall, judge_score_style, "
           "judge_score_quality, symbol_coverage_pct, outcome, reviewed_by, reviewed_at, policy_version, pr_url, recorded_at")


def fetch_rows(source) -> list[dict]:
    """The review rows. `source`: a list of row dicts, or a FactStore (Postgres: a SELECT through its connection helper; memory: its list)."""
    if isinstance(source, list):
        return source
    q = getattr(source, "_q", None)
    if q is not None:
        return q(f"SELECT {COLUMNS} FROM review_outcomes ORDER BY id")
    return list(getattr(source, "review_outcomes", []))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation in plain Python; None when n < 5 or either side has zero variance. With a 0/1 `ys` this is the point-biserial correlation."""
    n = len(xs)
    if n < MIN_CORR_N:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def segment_key(r: dict) -> str:
    return f"{r['diff_classification']}:{r['model_tier_used']}"


def is_synthetic(r: dict) -> bool:
    return str(r.get("pr_url", "")).startswith("synthetic://")


def segment_metrics(rows: list[dict], *, min_samples: int, threshold: float, z: float = 1.96) -> list[dict]:
    """One dict per (policy_version, diff_classification, model_tier_used). Synthetic rows are counted with the rest (the dashboards filter them by tag)."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["policy_version"], r["diff_classification"], r["model_tier_used"]), []).append(r)
    out = []
    for (policy, cls, tier), g in sorted(groups.items()):
        n = len(g)
        k = sum(r["outcome"] == "draft_with_noedition" for r in g)
        lo, hi = wilson_interval(k, n, z)
        corr = {}
        for s in SCORES:
            pairs = [(float(r[f"judge_score_{s}"]), 1.0 if r["outcome"] == "draft_with_noedition" else 0.0) for r in g
                     if r["outcome"] != "draft_rejected" and r.get(f"judge_score_{s}") is not None]
            corr[s] = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        out.append({"policy_version": policy, "diff_classification": cls, "model_tier_used": tier, "segment": f"{cls}:{tier}", "n": n, "noedition": k,
                    "edition": sum(r["outcome"] == "draft_with_edition" for r in g), "rejected": sum(r["outcome"] == "draft_rejected" for r in g),
                    "no_edition_rate": k / n, "wilson_lower_bound": lo, "wilson_upper_bound": hi, "z": z,
                    "graduation_progress": min(1.0, n / min_samples) if min_samples > 0 else 1.0, "graduation_n": n, "min_samples": min_samples, "threshold": threshold,
                    "ready": n >= min_samples and lo >= threshold, "judge_vs_human_correlation": corr,
                    "synthetic_n": sum(is_synthetic(r) for r in g)})
    return out


# ── deterministic ids ────────────────────────────────────────────────────────────
def _hex(*parts: str, length: int) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]


def review_ids(pr_url: str, change_unit_id: str) -> tuple[str, str]:
    return _hex("review", pr_url, change_unit_id, length=32), _hex("review-span", pr_url, change_unit_id, length=16)


def segment_ids(segment: str, policy_version: str, day: str) -> tuple[str, str]:
    return _hex("segment", segment, policy_version, day, length=32), _hex("segment-span", segment, policy_version, day, length=16)


def _iso(ts) -> str:
    if isinstance(ts, datetime):
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    return str(ts)


def _when(r: dict, fallback: datetime) -> str:
    return _iso(r.get("reviewed_at") or r.get("recorded_at") or fallback)


def review_span(r: dict, now: datetime) -> dict:
    trace_id, span_id = review_ids(r["pr_url"], r["change_unit_id"])
    t = _when(r, now)
    attrs = {"openinference.span.kind": "CHAIN", "session.id": segment_key(r), "input.value": r["change_unit_id"], "output.value": r["outcome"],
             "multisync.change_unit_id": r["change_unit_id"], "multisync.pr_url": r["pr_url"], "multisync.repo": r.get("repo"), "multisync.segment": segment_key(r),
             "multisync.diff_classification": r["diff_classification"], "multisync.tier": r["model_tier_used"], "multisync.policy_version": r["policy_version"],
             "multisync.outcome": r["outcome"], "multisync.reviewer": r.get("reviewed_by"), "multisync.synthetic": is_synthetic(r),
             "multisync.similarity_score": r.get("similarity_score"), "multisync.symbol_coverage_pct": r.get("symbol_coverage_pct"),
             **{f"multisync.judge.{s}": r.get(f"judge_score_{s}") for s in SCORES}}
    return {"name": SPAN_REVIEW, "context": {"trace_id": trace_id, "span_id": span_id}, "span_kind": "CHAIN", "parent_id": None, "start_time": t, "end_time": t,
            "status_code": "ERROR" if r["outcome"] == "draft_rejected" else "OK", "status_message": "", "attributes": {k: v for k, v in attrs.items() if v is not None}}


def segment_span(m: dict, now: datetime) -> dict:
    day = now.astimezone(timezone.utc).date().isoformat()
    trace_id, span_id = segment_ids(m["segment"], m["policy_version"], day)
    t = now.astimezone(timezone.utc).isoformat()
    attrs = {"openinference.span.kind": "CHAIN", "session.id": m["segment"], "multisync.segment": m["segment"], "multisync.policy_version": m["policy_version"],
             "multisync.date": day, "multisync.n": m["n"], "multisync.no_edition_rate": m["no_edition_rate"], "multisync.wilson_lower_bound": m["wilson_lower_bound"],
             "multisync.wilson_upper_bound": m["wilson_upper_bound"], "multisync.graduation_progress": m["graduation_progress"], "multisync.graduation_n": m["graduation_n"],
             "multisync.min_samples": m["min_samples"], "multisync.threshold": m["threshold"], "multisync.ready": m["ready"], "multisync.synthetic_n": m["synthetic_n"],
             "multisync.edition_n": m["edition"], "multisync.rejected_n": m["rejected"],
             **{f"multisync.judge_vs_human_correlation.{s}": v for s, v in m["judge_vs_human_correlation"].items()}}
    return {"name": SPAN_SEGMENT, "context": {"trace_id": trace_id, "span_id": span_id}, "span_kind": "CHAIN", "parent_id": None, "start_time": t, "end_time": t,
            "status_code": "OK", "status_message": "", "attributes": {k: v for k, v in attrs.items() if v is not None}}


def _log_spans(client, project: str, spans: list[dict]):
    """Phoenix's client RAISES (SpanCreationError) whenever any span already exists, even when the new ones were accepted in the same call, so a
    run with some new review rows and some old ones always raises. That is normal. It is only a failure when Phoenix also reports INVALID spans."""
    try:
        return client.spans.log_spans(project_identifier=project, spans=spans)
    except Exception as e:  # noqa: BLE001 - recognised by name so this module imports without the Phoenix client
        if type(e).__name__ != "SpanCreationError":
            raise
        invalid = getattr(e, "total_invalid", None)
        dup = getattr(e, "total_duplicates", None)
        if dup is None or invalid is None:  # an older client without the counts: fall back to the message
            import re

            m = re.search(r"Found (\d+) duplicate", str(e))
            dup, invalid = (int(m.group(1)) if m else 0), (1 if re.search(r"invalid", str(e), re.I) else 0)
        if invalid or not dup:
            raise  # a span Phoenix refused: never report it as a duplicate run
        return {"total_received": len(spans), "total_queued": max(0, len(spans) - dup), "total_duplicates": dup}


def _retry_until_ingested(call, attempts: int = 8, delay: float = 4.0, sleep=time.sleep):
    """Phoenix ingests spans asynchronously, so annotating a span right after logging it can answer 404. Wait for ingestion, a few times, then give up loudly."""
    for i in range(attempts):
        try:
            return call()
        except Exception as e:  # noqa: BLE001
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 404 or i == attempts - 1:
                raise
            sleep(delay)


def _log_annotations(client, annotations: list[dict]) -> int:
    """Annotation `review_segment_metrics` on each segment span: score = Wilson lower bound, label = ready/not_ready, the numbers in metadata. Upserted by Phoenix."""
    import pandas as pd

    df = pd.DataFrame(annotations)
    return len(client.spans.log_span_annotations_dataframe(dataframe=df, annotation_name=SPAN_SEGMENT, annotator_kind="CODE", sync=True))


def sync_review_evals(conn_or_factstore, phoenix_client, *, min_samples: int, threshold: float, dry_run: bool = False, project: str = "multirepo-agent-docs",
                      now: datetime | None = None, annotate=None) -> dict:
    """Read the review rows, compute the per-segment metrics and (unless dry_run) export both span kinds. dry_run touches no client at all."""
    now = now or datetime.now(timezone.utc)
    rows = fetch_rows(conn_or_factstore)
    metrics = segment_metrics(rows, min_samples=min_samples, threshold=threshold)
    result = {"rows": len(rows), "synthetic_rows": sum(is_synthetic(r) for r in rows), "segments": metrics, "dry_run": dry_run, "review_spans": 0, "segment_spans": 0, "duplicates": 0}
    if dry_run:
        return result
    r_spans = [review_span(r, now) for r in rows]
    s_spans = [segment_span(m, now) for m in metrics]
    for spans, key in ((r_spans, "review_spans"), (s_spans, "segment_spans")):
        if not spans:
            continue
        res = _log_spans(phoenix_client, project, spans)
        dup = (res or {}).get("total_duplicates", 0) if isinstance(res, dict) else 0
        result["duplicates"] += dup
        result[key] = len(spans) - dup
    if s_spans:
        notes = [{"span_id": s["context"]["span_id"], "label": "ready" if m["ready"] else "not_ready", "score": m["wilson_lower_bound"],
                  "explanation": f"n={m['n']}, no-edit rate {m['no_edition_rate']:.3f}, Wilson lower bound {m['wilson_lower_bound']:.3f} vs threshold {threshold}. Informational only.",
                  "metadata": {k: v for k, v in s["attributes"].items() if k.startswith("multisync.")}} for s, m in zip(s_spans, metrics)]
        _retry_until_ingested(lambda: (annotate or _log_annotations)(phoenix_client, notes))
    return result


def format_table(res: dict) -> str:
    head = f"{'segment':32} {'policy':14} {'n':>4} {'no-edit':>8} {'wilson lo':>10} {'progress':>9} {'ready':>6}  corr(precision,recall,style,quality)"
    lines = [head]
    for m in res["segments"]:
        c = ",".join("n/a" if m["judge_vs_human_correlation"][s] is None else f"{m['judge_vs_human_correlation'][s]:+.2f}" for s in SCORES)
        lines.append(f"{m['segment']:32} {m['policy_version'][:14]:14} {m['n']:>4} {m['no_edition_rate']:>8.3f} {m['wilson_lower_bound']:>10.3f} "
                     f"{m['graduation_progress']:>8.0%} {('yes' if m['ready'] else 'no'):>6}  {c}")
    lines.append(f"{res['rows']} review rows ({res['synthetic_rows']} synthetic). Informational only: nothing is auto-approved.")
    return "\n".join(lines)
