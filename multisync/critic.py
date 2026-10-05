"""The critic is the *role*; LLM-as-judge is the *technique* it uses for the expensive check.

One judge call returns per-claim verdicts plus per-fact coverage plus quality/style scores. Metrics are then computed in code so
thresholds stay deterministic and auditable:
  precision = supported claims / claims in draft   (hallucination guard)
  recall    = source facts covered / source facts  (completeness guard)
"""
from __future__ import annotations

from . import prompts as P


def _clamp01(n) -> float:
    try:
        v = float(n)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1.0, v))


def judge(llm, *, mode="docs", source="", draft="", known_facts=None, style_text="", existing="", changed_files=None,
          related_code="", fact_sheet="", plan="", tier=None) -> dict:
    """mode 'docs': source is a doc file. mode 'code': source is a code snapshot."""
    known_facts = known_facts or []
    changed_files = changed_files or []
    prompt = P.load_prompt("judge-code" if mode == "code" else "judge-docs")
    kf = "\n".join(known_facts) or "(none)"
    if mode == "code":
        user = (f"CODE:\n{source}\n\nCHANGED_FILES: {', '.join(changed_files) or '(unknown)'}\n\nEXISTING_PAGE:\n{existing or '(none)'}\n\n"
                f"RELATED_CODE:\n{related_code or '(none)'}\n\nFACT_SHEET:\n{fact_sheet or '(none)'}\n\nPLAN:\n{plan or '(none)'}\n\n"
                f"DRAFT:\n{draft}\n\nKNOWN_FACTS:\n{kf}\n\nSTYLE:\n{style_text or '(none)'}")
    else:
        user = f"SOURCE:\n{source}\n\nDRAFT:\n{draft}\n\nKNOWN_FACTS:\n{kf}\n\nSTYLE:\n{style_text or '(none)'}"
    messages = [{"role": "system", "content": prompt["text"]}, {"role": "user", "content": user}]
    try:
        r = llm.chat_json(messages, tier=tier)
    except ValueError:
        # A reply that is not valid JSON must not lose the whole page: ask once more, as the planning stages do.
        r = llm.chat_json([*messages, {"role": "user", "content": "Your previous reply was not valid JSON. Reply again with ONLY the JSON object."}], tier=tier)
    claims = r.get("claims") if isinstance(r.get("claims"), list) else []
    facts = r.get("facts") if isinstance(r.get("facts"), list) else []
    supported = sum(1 for c in claims if c.get("supported"))
    covered = sum(1 for f in facts if f.get("covered"))
    core = [f for f in facts if f.get("core")]
    return {
        "claims": claims,
        "precision": supported / len(claims) if claims else 1,
        "recall": covered / len(facts) if facts else 1,
        "coreRecall": sum(1 for f in core if f.get("covered")) / len(core) if core else 1,
        "missingCore": [f.get("text") for f in core if not f.get("covered")],
        "style": _clamp01(r.get("style")),
        "quality": _clamp01(r.get("quality")),
        "unsupported": [c.get("text") for c in claims if not c.get("supported")],
        "missing": [f.get("text") for f in facts if not f.get("covered")],
        "notes": [n for n in r.get("notes", []) if n] if isinstance(r.get("notes"), list) else [],
        "promptId": prompt["id"],
    }


def evaluate(m: dict, t) -> dict | None:
    """Map metrics + thresholds to the first failing check. Order = severity: hallucination first, because shipping a false
    claim is the worst outcome. Returns None or {tag, feedback}."""
    if m["precision"] < t["precisionMin"]:
        return {"tag": "hallucinated_claim", "feedback": [f'Unsupported claim, remove or correct: "{c}"' for c in m["unsupported"]]}
    # A critical omission fails no matter how good the overall ratio looks.
    if m.get("coreRecall", 1) < t["coreRecallMin"]:
        return {"tag": "missing_core_fact", "feedback": [f'Missing CORE fact, list it explicitly and completely: "{f}"' for f in m.get("missingCore") or []]}
    if m["recall"] < t["recallMin"]:
        return {"tag": "missing_claim", "feedback": [f'Missing fact from source: "{f}"' for f in m["missing"]]}
    if m["style"] < t["styleMin"]:
        return {"tag": "style_mismatch", "feedback": [f"Style score {m['style']:.2f} < {t['styleMin']}", *m["notes"]]}
    if m["quality"] < t["judgeMin"]:
        return {"tag": "judge_low_confidence", "feedback": [f"Quality score {m['quality']:.2f} < {t['judgeMin']}", *m["notes"]]}
    return None
