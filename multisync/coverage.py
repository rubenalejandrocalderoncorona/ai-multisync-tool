"""Deterministic completeness check, no LLM. For documentation types where completeness IS the point (a schema reference, a
configuration reference), the names declared in the code are extracted and the draft must mention each of them. The LLM judge
cannot do this reliably: it lists the facts it notices, so "recall 1.0" only means "complete against the facts it listed".

A style opts in:  "coverage": { "kinds": ["prisma_model", "prisma_enum"], "min": 0.9 }
"""
from __future__ import annotations

import re

EXTRACTORS = {
    "prisma_model": lambda t: re.findall(r"(?m)^model\s+([A-Za-z_]\w*)\s*\{", t),
    "prisma_enum": lambda t: re.findall(r"(?m)^enum\s+([A-Za-z_]\w*)\s*\{", t),
    "sql_table": lambda t: re.findall(r"(?i)create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`]?([A-Za-z_]\w*)[\"`]?", t),
    "env_var": lambda t: re.findall(r"\b(?:process\.env|import\.meta\.env|os\.environ(?:\.get)?\(?)[.\[\"']*([A-Z][A-Z0-9_]{2,})", t),
    "http_route": lambda t: re.findall(r"\b(?:app|router)\.(?:get|post|put|patch|delete)\(\s*[\"'`](/[^\"'`]*)[\"'`]", t),
    "graphql_type": lambda t: re.findall(r"(?m)^(?:type|input|enum|interface)\s+([A-Za-z_]\w*)", t),
}


def whole_word(text: str, name: str) -> bool:
    return re.search(rf"(^|[^A-Za-z0-9_]){re.escape(name)}($|[^A-Za-z0-9_])", text) is not None


def check_coverage(draft: str, code: str, spec: dict | None) -> dict | None:
    """None when the style does not opt in or nothing is declared in scope; otherwise {ok, kinds, missing, ratio, min}."""
    if not spec or not spec.get("kinds"):
        return None
    minimum = spec.get("min", 0.9)
    kinds: dict = {}
    missing: list[str] = []
    seen: set[str] = set()  # a name declared once counts once, even if two extractors match it
    found = total = 0
    for kind in spec["kinds"]:
        fn = EXTRACTORS.get(kind)
        if not fn:
            continue
        names = [n for n in dict.fromkeys(fn(code)) if n not in seen]
        seen.update(names)
        miss = [n for n in names if not whole_word(draft, n)]
        kinds[kind] = {"total": len(names), "found": len(names) - len(miss)}
        found += len(names) - len(miss)
        total += len(names)
        missing.extend(f"{kind}:{n}" for n in miss)
    if not total:
        return None  # nothing declared in scope: nothing to enforce
    ratio = found / total
    return {"ok": ratio >= minimum, "kinds": kinds, "missing": missing, "ratio": ratio, "min": minimum}
