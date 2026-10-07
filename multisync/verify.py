"""Deterministic draft verification. No model call: this is the cheap gate in front of the judge (and the reason a weak draft from
the cheap tier is caught before it costs a judge call or reaches a human).

  1. mentions      the draft names the public symbols the change touched
  2. front matter  the page composes valid front matter (title and description) and the draft did not add its own
  3. length        not empty, not truncated (unbalanced code fence, cut mid-sentence), not wildly shorter or longer than the existing page
"""
from __future__ import annotations

import re

from . import patching
from . import writer as W

PATCH_TOO_BROAD = "patch_too_broad"
SANE = {"minChars": 200, "maxChars": 120_000, "minRatioOfExisting": 0.25, "maxRatioOfExisting": 4, "mentionRatio": 0.6, "maxNamesChecked": 40}


def mentioned(text: str, name: str) -> bool:
    return re.search(rf"(^|[^A-Za-z0-9_$]){re.escape(name)}($|[^A-Za-z0-9_$])", text) is not None


def verify_draft(draft: str, names: list[str] | None = None, existing: str = "", file_path: str = "", title: str | None = None,
                 description: str | None = None, thresholds: dict | None = None, patch: dict | None = None) -> dict:
    """{ok, reasons, metrics}. `patch` (section-patch drafting only): {base, sectionsTotal, sectionsChanged, diffLines, maxShare, smallLines,
    minSections}; adds retainedPct and the patch_too_broad check."""
    t = {**SANE, **(thresholds or {})}
    reasons: list[str] = []
    metrics: dict = {}
    text = draft or ""

    # 1. mentions
    check = sorted(set(names or []))[: t["maxNamesChecked"]]
    if check:
        missing = [n for n in check if not mentioned(text, n)]
        metrics["mentioned"] = f"{len(check) - len(missing)}/{len(check)}"
        if (len(check) - len(missing)) / len(check) < t["mentionRatio"]:
            reasons.append(f"The draft does not mention the symbols this change touched. Name each of them: {', '.join(missing[:12])}{', ...' if len(missing) > 12 else ''}")

    # 2. front matter
    if re.match(r"^\s*---\s*\n", text):
        reasons.append("The draft starts with its own front matter. Output only the page body: front matter is added automatically.")
    else:
        page = W.with_frontmatter(text, file_path=file_path, source_url="x", commit="x", title=title, description=description)
        fm_m = re.match(r"^---\n([\s\S]*?)\n---", page)
        fm = fm_m.group(1) if fm_m else ""
        ttl = re.search(r'(?m)^title:\s*"(.*)"$', fm)
        desc = re.search(r'(?m)^description:\s*"(.*)"$', fm)
        metrics["frontMatter"] = "ok" if ttl and ttl.group(1) and desc and desc.group(1) else "incomplete"
        if not (ttl and ttl.group(1)):
            reasons.append("The page has no usable title.")
        if not (desc and desc.group(1)):
            reasons.append("The page has no usable description: start with a paragraph that says what the page is for.")

    # 3. length and completeness
    metrics["chars"] = len(text)
    if len(text) < t["minChars"]:
        reasons.append(f"The draft is only {len(text)} characters: far too short to be a page.")
    if len(text) > t["maxChars"]:
        reasons.append(f"The draft is {len(text)} characters: far too long. Keep it focused.")
    if len(re.findall(r"(?m)^```", text)) % 2 == 1:
        reasons.append("A code fence is never closed: the draft looks truncated.")
    last = (text.strip().split("\n") or [""])[-1] or ""
    if (len(text) >= t["minChars"] and last and not re.search(r"[.!?:)\]`|>*\-\d]$", last.strip())
            and not re.match(r"^(#{1,6}\s|\||[-*]\s|\d+\.\s|```|<)", last.strip())):
        reasons.append("The draft ends mid-sentence: it looks truncated.")
    if existing and len(existing) > 400:
        ratio = len(text) / len(existing)
        metrics["vsExisting"] = round(ratio, 2)
        if len(text) < len(existing) * t["minRatioOfExisting"]:
            reasons.append(f"The draft is {round(ratio * 100)}% the size of the existing page: content was dropped.")
        if len(text) > len(existing) * t["maxRatioOfExisting"]:
            reasons.append(f"The draft is {ratio:.1f}x the size of the existing page: check for repetition or padding.")
    if not re.search(r"(?m)^#{2,3}\s+\S", text):
        reasons.append("The draft has no section headings.")

    # 4. patch drift guard: a tiny source change must not make the model replace most of the page
    if patch:
        metrics["retainedPct"] = patching.retained_pct(patch["base"], text)
        total, changed = patch["sectionsTotal"], patch["sectionsChanged"]
        share = changed / total if total else 0
        if total >= patch["minSections"] and share > patch["maxShare"] and patch["diffLines"] <= patch["smallLines"]:
            reasons.append(f"{PATCH_TOO_BROAD}: the operations replaced or deleted {changed} of {total} sections ({round(share * 100)}%) for a source change of only "
                           f"{patch['diffLines']} line(s). Change only the sections whose facts this change contradicts or leaves missing, and leave every other section out of the list.")

    return {"ok": not reasons, "reasons": reasons, "metrics": metrics}
