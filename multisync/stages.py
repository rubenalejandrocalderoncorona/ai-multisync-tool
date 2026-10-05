"""The two LLM context stages that run before writing, code mode only."""
from __future__ import annotations

import json

from . import prompts as P


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _json_stage(llm, messages, **opts):
    """Retry once on malformed JSON: a stage must not silently continue with nothing."""
    try:
        return llm.chat_json(messages, **opts)
    except Exception as first:  # noqa: BLE001 - any parse/transport failure gets exactly one more try
        try:
            return llm.chat_json([*messages, {"role": "user", "content": "Your previous reply was not valid JSON. Reply again with ONLY the JSON object."}], **opts)
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"stage returned invalid JSON twice: {first}") from None


def analyze_code(llm, *, page, style_key, changed_files, repo_map, code, related, tier=None) -> dict:
    """STAGE 1: code context -> fact sheet. Returns {sheet, promptId, dropped}."""
    prompt = P.load_prompt("analyze-code")
    related_text = "\n\n".join(c["text"] for c in related) or "(none retrieved)"
    r = _json_stage(llm, [
        {"role": "system", "content": prompt["text"]},
        {"role": "user", "content": f"PAGE: {page} (style: {style_key or 'unspecified'})\n\nCHANGED_FILES: {', '.join(changed_files) or '(none)'}\n\n"
                                     f"REPO_MAP:\n{chr(10).join(repo_map) or '(unknown)'}\n\nCODE:\n{code}\n\nRELATED_CODE:\n{related_text}"},
    ], tier=tier)
    raw_facts = r.get("facts") if isinstance(r.get("facts"), list) else []
    facts = []
    for i, f in enumerate(raw_facts):
        if f and _str(f.get("text")) and _str(f.get("evidence")):  # a fact without evidence is not allowed
            facts.append({"id": _str(f.get("id")) or f"F{i + 1}", "text": f["text"], "evidence": f["evidence"],
                          "kind": _str(f.get("kind")) or "other", "status": _str(f.get("status")) or "unchanged"})
    unclear = [u for u in r["unclear"] if u] if isinstance(r.get("unclear"), list) else []
    return {"sheet": {"summary": _str(r.get("summary")), "facts": facts, "unclear": unclear}, "promptId": prompt["id"], "dropped": len(raw_facts) - len(facts)}


def plan_docs(llm, *, sheet, brief, style_text, existing, related, template, tier=None, repo_facts="") -> dict:
    """STAGE 2: semantic context -> documentation plan. Returns {plan, promptId}."""
    prompt = P.load_prompt("plan-docs")
    related_docs = "\n---\n".join(f"[{c.get('kind')}] {c.get('heading') or c.get('path')}: {c['text'][:700]}" for c in related) or "(none retrieved)"
    r = _json_stage(llm, [
        {"role": "system", "content": prompt["text"]},
        {"role": "user", "content": f"FACT_SHEET:\n{json.dumps(sheet, indent=1, ensure_ascii=False)}\n\nPAGE_BRIEF:\n{brief or '(none)'}\n\nSTYLE:\n{style_text or '(none)'}\n\n"
                                     f"REPO_FACTS (verified facts about the whole repository; use them for framing, do not contradict them):\n{repo_facts or '(none)'}\n\nEXISTING_PAGE:\n{existing or '(none)'}\n\nRELATED_DOCS:\n{related_docs}\n\nTEMPLATE:\n{template or '(none)'}"},
    ], tier=tier)
    ids = {f["id"] for f in sheet["facts"]}
    sections = []
    for s in r.get("sections") if isinstance(r.get("sections"), list) else []:
        sec = {"heading": _str(s.get("heading")), "action": _str(s.get("action")) or "add", "notes": _str(s.get("notes")),
               "must_cover": [i for i in (s.get("must_cover") if isinstance(s.get("must_cover"), list) else []) if i in ids]}  # drop invented ids
        if sec["heading"]:
            sections.append(sec)
    return {
        "plan": {
            "audience": _str(r.get("audience")), "purpose": _str(r.get("purpose")), "sections": sections,
            "terminology": r["terminology"] if isinstance(r.get("terminology"), list) else [],
            "out_of_scope": r["out_of_scope"] if isinstance(r.get("out_of_scope"), list) else [],
            "gaps": [g for g in r["gaps"] if g] if isinstance(r.get("gaps"), list) else [],
        },
        "promptId": prompt["id"],
    }


def sheet_text(sheet) -> str:
    return json.dumps(sheet, indent=1, ensure_ascii=False) if sheet else ""


def plan_text(plan) -> str:
    return json.dumps(plan, indent=1, ensure_ascii=False) if plan else ""
