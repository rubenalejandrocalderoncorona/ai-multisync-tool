"""The pure part of the draft evaluator: spans in, evaluator inputs out, scores in, flagged change units out. No network, no Phoenix, no pandas import.

A "draft" span (written by pipeline.n_write_draft) carries input.value (fact sheet + doc plan), output.value (the draft text), retrieval.documents.* (the chunks
the draft was written from), multisync.source.* (a bounded record of the source snapshot, see tracing.source_attrs) and multisync.change_unit_id / repo / page /
commit. The evaluator gives the judge the same evidence the writer had (source snapshot + retrieved chunks + fact sheet) and our own prompt (PROMPT below); the score is
the SHARE of supported claims about the documented component, in [0, 1], labelled faithful (>= 0.9), partial (>= 0.7) or unfaithful.
"""
from __future__ import annotations

import json
import re

SPAN_NAME = "draft"
EVAL_NAME = "draft_faithfulness"
DEFAULT_THRESHOLD = 0.7
FAITHFUL_AT = 0.9
PARTIAL_AT = 0.7
MAX_QUOTES = 10
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


def source_of(row: dict) -> dict:
    """The recorded source of a draft: text (may be absent: old span, or content capture was off), hash, size, files, changed files."""
    def lst(key):
        v = attr(row, key)
        try:
            v = json.loads(v) if isinstance(v, str) else v
        except ValueError:
            v = None
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

    return {"text": str(attr(row, "multisync.source.text") or ""), "sha256": attr(row, "multisync.source.sha256"), "chars": attr(row, "multisync.source.chars"),
            "files": lst("multisync.source.files"), "changed": lst("multisync.source.changed_files")}


def evidence_kind(source: dict) -> str:
    return "source+chunks" if source["text"] else "chunks_only"


def reference_text(row: dict, docs: list[str]) -> str:
    """Everything the judge may treat as true besides the fact sheet: the source snapshot the writer saw, then the retrieved chunks."""
    parts = []
    src = source_of(row)
    if src["text"]:
        changed = f" (changed in this commit: {', '.join(src['changed'])})" if src["changed"] else ""
        parts.append(f"[SOURCE FILES{changed}]\n{src['text']}")
    parts += [f"[chunk {i + 1}]\n{d}" for i, d in enumerate(docs)]
    return "\n\n".join(parts)


PROMPT = """You check a documentation page draft for HALLUCINATION against the evidence the writer was given. Be fair: the writer saw ALL of the evidence below.

Page: {page}   Repository: {repo}   (the draft documents this component only)

RULES
1. Judge ONLY claims about the component the page documents ({page} of {repo}). Ignore claims about other components, and ignore wording, tone, structure and style.
2. A claim is SUPPORTED if ANY of the evidence (source files, retrieved chunks, fact sheet / plan) supports it. Different components of the same repo can use different libraries or tiers: a fact about one component does not contradict a claim about another.
3. A claim is UNSUPPORTED only if it is contradicted by the evidence about THAT component, or no evidence mentions it at all. Reasonable paraphrase and summary are supported.
4. List each unsupported claim as a short quote (under 25 words) copied from the draft.
5. Count the checkable factual claims about this component ("claims"). Do not pad the count.

Reply with ONE JSON object and nothing else (no code fence, no prose):
{{"claims": <integer>, "unsupported": ["<short quote>", ...], "explanation": "<one or two sentences>"}}

=== FACT SHEET AND DOC PLAN (given to the writer) ===
{facts}

=== EVIDENCE ===
{evidence}

=== DRAFT TO CHECK ===
{draft}
"""
RETRY_NOTE = '\n\nYour previous reply was not the required JSON object. Reply again with ONLY: {"claims": <integer>, "unsupported": [<quotes>], "explanation": "..."}'


def build_prompt(info: dict, facts: str, evidence: str, draft: str) -> str:
    return PROMPT.format(page=info.get("page") or "(unknown page)", repo=info.get("repo") or "(unknown repo)", facts=facts or "(none)", evidence=evidence or "(none)", draft=draft)


def label_for(score: float) -> str:
    return "faithful" if score >= FAITHFUL_AT else "partial" if score >= PARTIAL_AT else "unfaithful"


_BARE_LABELS = {"faithful": 1.0, "partial": 0.8, "unfaithful": 0.0, "hallucinated": 0.0, "factual": 1.0}


def parse_verdict(text) -> dict:
    """The judge's reply as {claims, unsupported:[quotes], explanation, notes, score, label}. Accepts fenced JSON; a bare label becomes a result noted
    `judge_replied_with_label_only`. Raises ValueError for anything else (the caller retries once)."""
    raw = str(text or "").strip()
    bare = re.sub(r"[^a-z]", "", raw.lower())
    if bare in _BARE_LABELS:
        score = _BARE_LABELS[bare]
        return {"claims": None, "unsupported": [], "explanation": None, "notes": ["judge_replied_with_label_only"], "score": score, "label": label_for(score)}
    clean = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    clean = re.sub(r"\s*```$", "", clean).strip()
    obj = None
    found = re.search(r"\{[\s\S]*\}", clean)
    for candidate in (clean, found.group(0) if found else None):
        if candidate:
            try:
                obj = json.loads(candidate)
                break
            except ValueError:
                continue
    if not isinstance(obj, dict) or "claims" not in obj:
        raise ValueError(f"judge reply is not the expected JSON: {raw[:120]!r}")
    try:
        claims = max(0, int(obj["claims"]))
    except (TypeError, ValueError):
        raise ValueError(f"judge reply has a non-integer claims: {obj['claims']!r}") from None
    raw_q = obj.get("unsupported")
    quotes = [str(q).strip()[:200] for q in (raw_q if isinstance(raw_q, list) else []) if str(q).strip()]
    notes = []
    if isinstance(raw_q, int) and not isinstance(raw_q, bool):  # a count instead of quotes
        n_bad = max(0, raw_q)
        notes.append("unsupported_given_as_count")
    else:
        n_bad = len(quotes)
    n_bad = min(n_bad, claims)
    if claims == 0:
        score, notes = 1.0, notes + ["no_claims_judged"]
    else:
        score = (claims - n_bad) / claims
    return {"claims": claims, "unsupported": quotes[:MAX_QUOTES], "unsupported_count": n_bad, "explanation": obj.get("explanation"), "notes": notes,
            "score": round(score, 3), "label": label_for(score)}


def judge_row(row: dict, judge, retries: int = 1) -> dict | None:
    """Ask the judge about one built row; at most `retries` re-asks when the reply is not parseable. None when it never was (or the call failed)."""
    prompt = row["prompt"]
    for attempt in range(retries + 1):
        try:
            result = parse_verdict(judge.complete(prompt if attempt == 0 else prompt + RETRY_NOTE))
        except ValueError:
            continue
        except Exception:  # noqa: BLE001 - a transport failure scores nothing for this span, never the whole run
            return None
        result["model"] = getattr(judge, "model", None)
        return result
    return None


def evaluate(rows: list[dict], judge) -> tuple[list[dict], str]:
    """Score the built rows. Returns the rows with a `faithfulness_score` result dict (None where the judge failed)."""
    return [{**r, "faithfulness_score": judge_row(r, judge)} for r in rows], "faithfulness_score"


def span_info(row: dict) -> dict:
    """Which change unit a span belongs to. change_unit_id has the same shape review_outcomes uses: <repo>@<commit>:<page>."""
    repo, commit, page = (str(attr(row, f"multisync.{k}") or "") for k in ("repo", "commit", "page"))
    cuid = attr(row, "multisync.change_unit_id") or (f"{repo}@{commit}:{page}" if repo and commit and page else None)
    return {"span_id": span_id_of(row), "change_unit_id": cuid, "repo": repo or None, "commit": commit or None, "page": page or None,
            "attempt": attr(row, "multisync.attempt"), "tier": attr(row, "multisync.tier")}


def build_inputs(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Evaluator rows ({span_id, input, output, context} plus the change unit) and the reasons spans were left out.
    input = the fact sheet + doc plan the draft was asked to follow, output = the draft, context = source snapshot + retrieved chunks, prompt = what the judge is asked."""
    built, skipped = [], []
    for row in rows:
        info = span_info(row)
        if not info["span_id"]:
            skipped.append("a span without a span id")
        elif not _present(attr(row, "output.value")):
            skipped.append(f"{info['span_id']}: no draft text")
        elif not documents_of(row) and not source_of(row)["text"]:
            skipped.append(f"{info['span_id']}: no retrieved chunks or source to ground it in")
        else:
            docs, src = documents_of(row), source_of(row)
            facts, draft, evidence = str(attr(row, "input.value") or ""), str(attr(row, "output.value")), reference_text(row, docs)
            built.append({**info, "input": facts, "output": draft, "context": evidence, "prompt": build_prompt(info, facts, evidence, draft), "evidence": evidence_kind(src),
                          "evidence_chars": len(evidence), "source_chars": len(src["text"]), "chunks": len(docs), "source_files": len(src["files"]),
                          "source_hash_only": bool(src["sha256"]) and not src["text"]})
    return built, skipped


def annotations_from_scores(rows: list[dict], score_column: str) -> tuple[list[dict], list[str]]:
    """One Phoenix span annotation per scored row. `score_column` holds the judge result (see parse_verdict); a row the judge failed on has none."""
    out, failed = [], []
    for r in rows:
        res = r.get(score_column)
        res = json.loads(res) if isinstance(res, str) else res
        if not isinstance(res, dict) or res.get("score") is None:
            failed.append(r["span_id"])
            continue
        notes = list(res.get("notes") or [])
        if r.get("evidence") == "chunks_only":
            notes.append("source_not_recorded" if r.get("source_hash_only") else "old_span_without_source")
        out.append({"span_id": r["span_id"], "name": EVAL_NAME, "annotator_kind": "LLM", "label": res.get("label") or label_for(float(res["score"])), "score": float(res["score"]),
                    "explanation": res.get("explanation"),
                    "metadata": {"judge": res.get("model"), "change_unit_id": r.get("change_unit_id"), "claims": res.get("claims"),
                                 "unsupported": res.get("unsupported_count", len(res.get("unsupported") or [])), "quotes": list(res.get("unsupported") or [])[:MAX_QUOTES],
                                 "evidence": r.get("evidence") or "chunks_only", "evidence_chars": r.get("evidence_chars"), "notes": notes}})
    return out, failed


def _metadata(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    return v if isinstance(v, dict) else {}


def annotation_records(rows: list[dict]) -> list[dict]:
    """Logged evaluations as flat {span_id, score, label, explanation} (get_span_annotations_dataframe nests the result under `result.`)."""
    out = []
    for r in rows:
        score = r.get("result.score", r.get("score"))
        if _present(score) and r.get("annotation_name", r.get("name", EVAL_NAME)) == EVAL_NAME:
            out.append({"span_id": span_id_of(r), "score": float(score), "label": r.get("result.label", r.get("label")),
                        "explanation": r.get("result.explanation", r.get("explanation")), "metadata": _metadata(r.get("metadata"))})
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
            flagged.append({**i, "score": a["score"], "label": a["label"], "explanation": a["explanation"], "metadata": a.get("metadata") or {}, "pr": pr_link(i, pr_urls)})
    return sorted(flagged, key=lambda f: (f["score"], str(f["change_unit_id"])))


def format_findings(flagged: list[dict], threshold: float, scored: int) -> str:
    if not flagged:
        return f"no draft scored below {threshold} ({scored} scored)"
    lines = [f"{len(flagged)} of {scored} scored drafts below {threshold}:"]
    for f in flagged:
        lines += [f"  {f['score']:.2f}  {f['change_unit_id']}", f"        repo {f['repo']}   page {f['page']}   attempt {f['attempt'] or '-'}   span {f['span_id']}",
                  f"        link {f['pr'] or '-'}"]
        m = f.get("metadata") or {}
        if m.get("claims") is not None:
            lines.append(f"        {m.get('unsupported')} of {m['claims']} claims unsupported; evidence {m.get('evidence') or '?'}" + ("  (weaker score: chunks only)" if m.get("evidence") == "chunks_only" else ""))
        lines += [f"        quote  {' '.join(str(q).split())[:200]}" for q in (m.get("quotes") or [])[:5]]
        if f.get("explanation"):
            lines.append(f"        why  {' '.join(str(f['explanation']).split())[:300]}")
    return "\n".join(lines)
