"""Zero-LLM structural analysis. No tokens are spent here.

Two outputs matter to the pipeline:
  forced   the *shape* changed (a heading, code block, table row, list item, function or array element was added/removed).
           Shape changes override embedding similarity, because a one-item addition barely moves a vector.
  changed  shape or fact-bearing tokens (numbers, URLs, inline code, env vars) changed. Pure rewording with identical facts
           is not worth generation cost.

A lightweight lexical signature, not a parser; language-agnostic so one implementation covers Markdown and common source files.
"""
from __future__ import annotations

import re

CODE_EXT = re.compile(r"\.(js|jsx|ts|tsx|mjs|cjs|py|go|java|rb|rs|yaml|yml|json)$", re.I)

_FUNC_RES = [
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*=>"),
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M),
]


def signature(content: str | None) -> dict:
    text = content or ""
    lines = text.split("\n")
    headings = [l.strip() for l in lines if re.match(r"^#{1,6}\s+\S", l)]
    fences = len(re.findall(r"(?m)^```", text)) / 2
    table_rows = sum(1 for l in lines if re.match(r"^\s*\|.*\|\s*$", l) and not re.match(r"^\s*\|[\s:|-]+\|\s*$", l))
    list_items = sum(1 for l in lines if re.match(r"^\s*([-*+]|\d+\.)\s+\S", l))
    functions = set()
    for rx in _FUNC_RES:
        for m in rx.finditer(text):
            functions.add(m.group(1))
    arrays = {}
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=\s*\[([^\]]*)\]", text):
        arrays[m.group(1)] = len([s for s in m.group(2).split(",") if s.strip()])
    tokens = set(re.findall(r"`[^`\n]+`", text))
    tokens |= set(re.findall(r"https?://[^\s)>\"']+", text))
    tokens |= set(re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text))
    tokens |= set(re.findall(r"\b\d+(?:\.\d+)*\b", text))
    tokens |= set(re.findall(r"\b\d+(?:\.\d+)?(?:ms|s|sec|m|min|h|hr|d)\b", text))  # durations: 30m, 72h, 500ms
    tokens |= {n.replace("_", "") for n in re.findall(r"\b\d{1,3}(?:_\d{3})+\b", text)}  # 10_000 is the number 10000 (an underscore hides it from \b\d+\b)
    return {"headings": headings, "fences": fences, "tableRows": table_rows, "listItems": list_items,
            "functions": sorted(functions), "arrays": arrays, "tokens": sorted(tokens)}


def structural_change(before: str, after: str) -> dict:
    a, b = signature(before), signature(after)
    shape, facts = [], []
    if a["headings"] != b["headings"]:
        shape.append("headings")
    if a["fences"] != b["fences"]:
        shape.append("code_blocks")
    if a["tableRows"] != b["tableRows"]:
        shape.append("table_rows")
    if a["listItems"] != b["listItems"]:
        shape.append("list_items")
    if a["functions"] != b["functions"]:
        shape.append("functions")
    if a["arrays"] != b["arrays"]:
        shape.append("array_literals")
    if a["tokens"] != b["tokens"]:
        facts.append("fact_tokens")
    return {
        "changed": bool(shape or facts),
        "forced": bool(shape),
        "reasons": shape + facts,
        "symbols": {"added": [f for f in b["functions"] if f not in a["functions"]], "tokens": b["tokens"]},
    }


def diff_line_count(before: str | None, after: str | None) -> dict:
    """Added + removed non-blank lines, order-insensitive (multiset diff)."""
    bag: dict[str, int] = {}
    for l in [s.strip() for s in (before or "").split("\n") if s.strip()]:
        bag[l] = bag.get(l, 0) + 1
    added = 0
    for l in [s.strip() for s in (after or "").split("\n") if s.strip()]:
        n = bag.get(l, 0)
        if n > 0:
            bag[l] = n - 1
        else:
            added += 1
    removed = sum(bag.values())
    return {"added": added, "removed": removed, "total": added + removed}
