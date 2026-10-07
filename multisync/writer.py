"""Generation side: GAR, template selection, drafting, polish, folder classification, and Starlight frontmatter.
All LLM calls go through the injected `llm`."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from . import patching
from . import prompts as P


def generate_hypothetical(llm, *, file_path, before, after, mode="docs", changed_files=None) -> str:
    """GAR: describe the change as a hypothetical doc paragraph; its embedding drives retrieval. Never indexed."""
    changed_files = changed_files or []
    if mode == "code":
        user = f"Changed files: {', '.join(changed_files) or '(unknown)'}\n\nCODE AFTER:\n{after[:6000]}"
    else:
        user = f"File: {file_path}\n\nBEFORE:\n{(before or '(new file)')[:2500]}\n\nAFTER:\n{after[:2500]}"
    return llm.chat([{"role": "system", "content": P.load_prompt("gar")["text"]}, {"role": "user", "content": user}], fast=True)


def generate_gar_from_facts(llm, *, sheet, brief) -> dict:
    """GAR from the fact sheet: several hypothetical documentation paragraphs (one per topic) written AFTER the code analysis, so
    they describe what the docs should say about verified facts rather than guess from raw code. Each paragraph becomes one
    retrieval query against the documentation index. Never published, never indexed. A failure here only degrades retrieval, so it
    returns [] instead of failing the run."""
    prompt = P.load_prompt("gar-facts")
    try:
        facts = [{"id": f["id"], "text": f["text"], "kind": f["kind"], "status": f["status"]} for f in sheet["facts"]]
        r = llm.chat_json([
            {"role": "system", "content": prompt["text"]},
            {"role": "user", "content": f"FACT_SHEET:\n{json.dumps(facts, indent=1, ensure_ascii=False)}\n\nPAGE_BRIEF:\n{brief or '(none)'}"},
        ], fast=True)
        paras = r.get("paragraphs") if isinstance(r.get("paragraphs"), list) else []
        return {"paragraphs": [x for x in paras if isinstance(x, str) and x.strip()][:4], "promptId": prompt["id"]}
    except Exception as e:  # noqa: BLE001
        return {"paragraphs": [], "promptId": prompt["id"], "error": str(e)}


def find_template_files(directory: str) -> list[str]:
    out: list[str] = []
    if not os.path.exists(directory):
        return out
    for e in sorted(os.scandir(directory), key=lambda x: x.name):
        if e.is_dir():
            out.extend(find_template_files(e.path))
        elif re.search(r"\.(md|mdx)$", e.name):
            out.append(e.path)
    return out


def select_template(llm, *, file_path, content, template_files, default_template):
    candidates = [f for f in template_files if not f.endswith("default-template.md")]
    if not candidates:
        return default_template
    lines = []
    for f in candidates:
        body = re.sub(r"^---[\s\S]*?---\n+", "", open(f, encoding="utf-8").read(), count=1)
        first = next((l for l in body.split("\n") if l.strip()), "")
        heading = re.sub(r"^#+\s*", "", first)
        lines.append(f"- {f}: {heading}")
    catalogue = "\n".join(lines)
    choice = llm.chat([
        {"role": "system", "content": f"Pick the single best template for the document.\n{catalogue}\nReply ONLY with the exact path, or DEFAULT."},
        {"role": "user", "content": f"Filename: {os.path.basename(file_path)}\n\n{content[:3000]}"},
    ], fast=True)
    return next((f for f in candidates if f == choice or f.endswith(re.sub(r"^\./", "", choice))), default_template)


def draft_document(llm, *, mode="docs", file_path, source, existing="", changed_files=None, related_code="", fact_sheet="", plan="",
                   template_path=None, context=None, policy=None, style=None, instructions="", feedback=None, tier=None) -> dict:
    """Produce a draft. `feedback` carries the judge's findings on retry.
    mode 'docs': source is a documentation file. mode 'code': source is a code snapshot and `existing` is the live page."""
    changed_files = changed_files or []
    context = context or []
    policy = policy or {}
    # Code mode follows the style's own outline; a generic business template would demand sections the code cannot support.
    if mode == "code" and style and style.get("outline"):
        template = P.outline_text(style)
    elif template_path and os.path.exists(template_path):
        template = open(template_path, encoding="utf-8").read()
    else:
        template = ""
    ctx = "\n---\n".join(f"[{c['heading']}] {c['text'][:600]}" for c in context)
    fix = "\n\nA reviewer rejected the previous attempt. Fix exactly these problems:\n- " + "\n- ".join(feedback) if feedback else ""
    prompt = P.load_prompt("draft-code" if mode == "code" else "draft-docs")
    system = "\n\n".join(p for p in [
        P.fill(prompt["text"], {"PERSONA": (style or {}).get("prompt") or "You are a senior technical writer."}),
        instructions and f"Documentation standards:\n{instructions}",
        P.style_text({"key": None, "rubric": []}, policy),
    ] if p)
    if mode == "code":
        user = (f"TEMPLATE:\n{template}\n\nCONTEXT:\n{ctx or '(none)'}\n\nEXISTING_PAGE:\n{existing or '(none: write a new page)'}\n\n"
                f"CHANGED_FILES: {', '.join(changed_files)}\n\nFACT_SHEET:\n{fact_sheet or '(none)'}\n\nPLAN:\n{plan or '(none)'}\n\n"
                f"CODE:\n{source}\n\nRELATED_CODE:\n{related_code or '(none)'}{fix}")
    else:
        user = f"TEMPLATE:\n{template}\n\nCONTEXT:\n{ctx or '(none)'}\n\nSOURCE ({os.path.basename(file_path)}):\n{source}{fix}"
    out = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], tier=tier)
    text = out if len(out) > 50 else ((existing or out) if mode == "code" else source)
    return {"text": text, "promptId": prompt["id"]}


def patch_document(llm, *, sections, file_path=None, changed_files=None, changed_symbols=None, diff_text="", related_code="", fact_sheet="", plan="", source="",
                   context=None, policy=None, style=None, instructions="", feedback=None, tier=None, mode="code", reviewer_request="") -> dict:
    """Section-patch drafting (mode "code": prompt patch-code, `source` is a code snapshot; mode "docs": prompt patch-docs, `source` is the edited
    markdown document and `diff_text` its diff). `reviewer_request` (a revision after review) replaces the source diff as WHAT_CHANGED. The model returns operations on the existing sections, code assembles the page. Malformed JSON is retried once.
    Raises patching.PatchError when the reply cannot be used (still malformed, unknown section id, bad operation): the caller falls back to
    draft_document. Returns {text, promptId, operations, changed: {replaced, deleted, inserted}, unchangedReason}."""
    changed_files = changed_files or []
    prompt = P.load_prompt("patch-docs" if mode == "docs" else "patch-code")
    system = "\n\n".join(p for p in [
        P.fill(prompt["text"], {"PERSONA": (style or {}).get("prompt") or "You are a senior technical writer."}),
        instructions and f"Documentation standards:\n{instructions}",
        P.style_text({"key": None, "rubric": []}, policy or {}),
    ] if p)
    ctx = "\n---\n".join(f"[{c['heading']}] {c['text'][:600]}" for c in (context or []))
    fix = "\n\nA reviewer rejected the previous attempt. Fix exactly these problems:\n- " + "\n- ".join(feedback) if feedback else ""
    blocks = "\n".join(f"=== SECTION {s['id']} ===\n{s['text'].rstrip(chr(10))}\n" for s in sections)
    if reviewer_request:
        what = f"REVIEWER_REQUEST (a reviewer asked for these changes to the page; the source did not change):\n{reviewer_request}"
    elif mode == "docs":
        what = f"Source document: {file_path or '(unknown)'}\nDiff of the source document (before -> after):\n{diff_text or '(not available)'}"
    else:
        what = (f"Changed files: {', '.join(changed_files) or '(unknown)'}\nChanged public symbols: {', '.join(changed_symbols or []) or '(none detected)'}\n"
                f"Source diff (before -> after):\n{diff_text or '(not available)'}")
    if mode == "docs":
        tail = f"SOURCE_DOCUMENT:\n{source}"
    else:
        tail = f"FACT_SHEET:\n{fact_sheet or '(none)'}\n\nPLAN:\n{plan or '(none)'}\n\nCODE:\n{source}\n\nRELATED_CODE:\n{related_code or '(none)'}"
    user = f"WHAT_CHANGED:\n{what}\n\nSECTIONS:\n{blocks}\nCONTEXT:\n{ctx or '(none)'}\n\n{tail}{fix}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        r = llm.chat_json(messages, tier=tier)
    except ValueError:
        r = llm.chat_json([*messages, {"role": "user", "content": "Your previous reply was not valid JSON. Reply again with ONLY the JSON object."}], tier=tier)
    ops = r if isinstance(r, list) else (r.get("operations") if isinstance(r, dict) else None)
    if not isinstance(ops, list):
        raise patching.PatchError("reply has no operations list")
    text = patching.apply_operations(sections, ops)
    return {"text": text, "promptId": prompt["id"], "operations": ops, "changed": patching.changed_ids(sections, ops),
            "unchangedReason": (r.get("unchanged_reason") if isinstance(r, dict) else None) or None}


def polish_only(llm, *, draft, policy, style, instructions, tier=None) -> str:
    """Polish-only rewrite; forbidden from touching facts (failure mode #8)."""
    prompt = P.load_prompt("polish")
    system = P.fill(prompt["text"], {"STYLE": "\n\n".join(p for p in [P.style_text(style or {"rubric": []}, policy), instructions] if p)})
    out = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": draft}], tier=tier)
    return out if len(out) > 50 else draft


def classify_folder(llm, *, file_path, content, folder_spec) -> str | None:
    spec = (f"Use exactly one folder slug from:\n{folder_spec}" if folder_spec else
            "Use one of: how-to-guides, configuration-field-reference, features, setup-guides, reference, concepts, tutorials, troubleshooting. Reply ROOT for a top-level overview.")
    choice = llm.chat([
        {"role": "system", "content": f"Classify the document into a docs subfolder. {spec}\nReply with ONLY the slug."},
        {"role": "user", "content": f"Filename: {os.path.basename(file_path)}\n\n{content[:2000]}"},
    ], fast=True).lower()
    if not choice or choice == "root":
        return None
    slug = re.sub(r"^-|-$", "", re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", choice)))
    return slug or None


def extract_folder_spec(instructions: str | None) -> str | None:
    m = re.search(r"(?i)##\s+Folder Structure\s*\n([\s\S]*?)(?=\n##\s|\s*\Z)", instructions or "")
    return m.group(1).strip() if m else None


def title_from(content: str, file_path: str) -> str:
    h = re.search(r"(?m)^#{1,2}\s+(.+)$", content)
    if h:
        return re.sub(r"[`*_]", "", h.group(1)).strip()
    name = re.sub(r"[_-]", " ", os.path.splitext(os.path.basename(file_path))[0])
    return name[:1].upper() + name[1:]


def description_from(content: str) -> str:
    body = re.sub(r"(?m)^#.*$", "", content)
    para = next((p for p in (x.strip() for x in re.split(r"\n{2,}", body)) if p and not re.match(r"^[|`>:\-*\d]", p)), "")
    return re.sub(r"\s+", " ", para)[:160].replace('"', '\\"')


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def with_frontmatter(content: str, *, file_path: str, source_url: str, commit: str, doc_key: str | None = None,
                     title: str | None = None, description: str | None = None) -> str:
    """Starlight-compatible frontmatter (title + description are what its schema expects)."""
    body = re.sub(r"^---\n[\s\S]*?\n---\n+", "", content, count=1)
    ttl = (title or title_from(body, file_path)).replace('"', '\\"')
    cleaned = re.sub(r"^#\s+.+\n+", "", body, count=1)  # Starlight renders `title` as the page H1; drop a duplicate leading H1
    desc = re.sub(r"\s+", " ", description)[:160].replace('"', '\\"') if description else description_from(cleaned)
    return (f'---\ntitle: "{ttl}"\ndescription: "{desc}"\nsource: {source_url}\ndoc_key: {doc_key or file_path}\n'
            f"commit: {commit}\nlast_synced: {_iso_now()}\nautomated: true\n---\n\n{cleaned}")


def with_change_history(content: str, *, repo: str, commit: str, gaps: list[str] | None = None, now: datetime | None = None) -> str:
    """The Change History table is generated by code, never by the model: a model asked for a date invents one.
    Any table the model wrote is removed first."""
    gaps = gaps or []
    now = now or datetime.now(timezone.utc)
    stripped = re.sub(r"(?i)\n#{2,3}\s+Change History\b[\s\S]*?(?=\n#{2,3}\s+(?!Change History)\S|\Z)", "", content).rstrip()
    completeness = ("Partially filled: the source does not show " + "; ".join(gaps[:3]).replace("|", "/")) if gaps else "Fully filled"
    row = f"| {now.strftime('%Y-%m-%d')} | {commit[:7]} | Documentation Bot | Generated from {repo} at {commit[:7]} | {completeness} |"
    return (f"{stripped}\n\n## Change History\n\n| Date | Version | Author | Change Description | Completeness |\n"
            f"|------|---------|--------|--------------------|--------------|\n{row}\n")
