"""The pure part of the draft evaluator: spans in, evaluator inputs out, scores in, flagged change units out. No network, no Phoenix, no pandas import.

A "draft" span (written by pipeline.n_write_draft) carries input.value (fact sheet + doc plan), output.value (the draft text), retrieval.documents.* (the chunks
the draft was written from) and multisync.change_unit_id / repo / page / commit. The evaluator scores each one with Phoenix's FaithfulnessEvaluator, which grounds
the draft in a reference text (a draft that says something no chunk or fact supports is a hallucination: score 0.0, label "unfaithful"; 1.0 is "faithful").
"""
from __future__ import annotations

import json
import re

SPAN_NAME = "draft"
EVAL_NAME = "draft_faithfulness"
DEFAULT_THRESHOLD = 0.7
SPAN_ID = "context.span_id"
_DOC_COLUMN = re.compile(r"^(?:attributes\.)?retrieval\.documents\.(\d+)\.document\.content$")


def _present(v) -> bool:
    return v is not None and not (isinstance(v, float) and v != v) and v != ""


def attr(row: dict, key: str):
    """An attribute of a span row. Phoenix returns it as `key` when selected and as `attributes.key` in the default dataframe."""
    for k in (key, f"attributes.{key}"):
        if _present(row.get(k)):
            return row[k]
    return None


def span_id_of(row: dict) -> str | None:
    return next((str(row[k]) for k in (SPAN_ID, "span_id") if _present(row.get(k))), None)


def documents_of(row: dict) -> list[str]:
    """The text of the retrieved chunks, in order, from either shape Phoenix returns: one list column, or one flattened column per chunk."""
    docs = attr(row, "retrieval.documents")
    if isinstance(docs, str):
        try:
            docs = json.loads(docs)
        except ValueError:
            docs = None
    out: list[str] = []
    if isinstance(docs, (list, tuple)) or hasattr(docs, "tolist"):
        for d in list(docs.tolist() if hasattr(docs, "tolist") else docs):
            if isinstance(d, dict):
                inner = d.get("document") if isinstance(d.get("document"), dict) else d
                text = d.get("document.content") or inner.get("content")
            else:
                text = d
            if _present(text):
                out.append(str(text))
        return out
    flat = sorted(((int(m.group(1)), v) for k, v in row.items() if (m := _DOC_COLUMN.match(str(k))) and _present(v)), key=lambda x: x[0])
    return [str(v) for _, v in flat]


def reference_text(row: dict, docs: list[str]) -> str:
    """What the judge may treat as true: the chunks the draft was written from."""
    return "\n\n".join(f"[chunk {i + 1}]\n{d}" for i, d in enumerate(docs))


def span_info(row: dict) -> dict:
    """Which change unit a span belongs to. change_unit_id has the same shape review_outcomes uses: <repo>@<commit>:<page>."""
    repo, commit, page = (str(attr(row, f"multisync.{k}") or "") for k in ("repo", "commit", "page"))
    cuid = attr(row, "multisync.change_unit_id") or (f"{repo}@{commit}:{page}" if repo and commit and page else None)
    return {"span_id": span_id_of(row), "change_unit_id": cuid, "repo": repo or None, "commit": commit or None, "page": page or None,
            "attempt": attr(row, "multisync.attempt"), "tier": attr(row, "multisync.tier")}


def build_inputs(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Evaluator rows ({span_id, input, output, context} plus the change unit) and the reasons spans were left out.
    input = the fact sheet + doc plan the draft was asked to follow, output = the draft, context = the retrieved chunks."""
    built, skipped = [], []
    for row in rows:
        info = span_info(row)
        if not info["span_id"]:
            skipped.append("a span without a span id")
        elif not _present(attr(row, "output.value")):
            skipped.append(f"{info['span_id']}: no draft text")
        elif not documents_of(row):
            skipped.append(f"{info['span_id']}: no retrieved chunks to ground it in")
        else:
            built.append({**info, "input": str(attr(row, "input.value") or ""), "output": str(attr(row, "output.value")), "context": reference_text(row, documents_of(row))})
    return built, skipped


def annotations_from_scores(rows: list[dict], score_column: str) -> tuple[list[dict], list[str]]:
    """One Phoenix span annotation per scored row. `score_column` holds the JSON an evaluate_dataframe Score serialises to; a row the judge failed on has none."""
    out, failed = [], []
    for r in rows:
        raw = r.get(score_column)
        scores = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(scores, list):
            scores = scores[0] if scores else None
        if not isinstance(scores, dict) or scores.get("score") is None:
            failed.append(r["span_id"])
            continue
        out.append({"span_id": r["span_id"], "name": EVAL_NAME, "annotator_kind": "LLM", "label": scores.get("label"), "score": float(scores["score"]),
                    "explanation": scores.get("explanation"), "metadata": {"judge": (scores.get("metadata") or {}).get("model"), "change_unit_id": r.get("change_unit_id")}})
    return out, failed


def annotation_records(rows: list[dict]) -> list[dict]:
    """Logged evaluations as flat {span_id, score, label, explanation} (get_span_annotations_dataframe nests the result under `result.`)."""
    out = []
    for r in rows:
        score = r.get("result.score", r.get("score"))
        if _present(score) and r.get("annotation_name", r.get("name", EVAL_NAME)) == EVAL_NAME:
            out.append({"span_id": span_id_of(r), "score": float(score), "label": r.get("result.label", r.get("label")),
                        "explanation": r.get("result.explanation", r.get("explanation"))})
    return out


def pr_link(info: dict, pr_urls: dict | None) -> str | None:
    """The review PR when review_outcomes knows it, else the source commit (the PR is opened after the run, so a span never knows it)."""
    if pr_urls and pr_urls.get(info.get("change_unit_id")):
        return pr_urls[info["change_unit_id"]]
    return f"https://github.com/{info['repo']}/commit/{info['commit']}" if info.get("repo") and info.get("commit") else None


def flag_low(annotations: list[dict], span_rows: list[dict], threshold: float, pr_urls: dict | None = None) -> list[dict]:
    """Every scored draft strictly below the threshold, worst first, with the change unit it belongs to."""
    info = {i["span_id"]: i for i in map(span_info, span_rows)}
    flagged = []
    for a in annotations:
        if a["score"] < threshold and a["span_id"] in info:
            i = info[a["span_id"]]
            flagged.append({**i, "score": a["score"], "label": a["label"], "explanation": a["explanation"], "pr": pr_link(i, pr_urls)})
    return sorted(flagged, key=lambda f: (f["score"], str(f["change_unit_id"])))


def format_findings(flagged: list[dict], threshold: float, scored: int) -> str:
    if not flagged:
        return f"no draft scored below {threshold} ({scored} scored)"
    lines = [f"{len(flagged)} of {scored} scored drafts below {threshold}:"]
    for f in flagged:
        lines += [f"  {f['score']:.2f}  {f['change_unit_id']}", f"        repo {f['repo']}   page {f['page']}   attempt {f['attempt'] or '-'}   span {f['span_id']}",
                  f"        link {f['pr'] or '-'}"]
        if f.get("explanation"):
            lines.append(f"        why  {' '.join(str(f['explanation']).split())[:300]}")
    return "\n".join(lines)
