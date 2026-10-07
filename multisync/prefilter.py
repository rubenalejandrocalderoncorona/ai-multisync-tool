"""Layer 1 of the cheap-to-expensive cascade. Returns a verdict without touching the network."""
from __future__ import annotations

import re

from .structure import diff_line_count, structural_change
from .symbols import diff_public_symbols


_STRING = re.compile(r'"([^"\\\n]{6,})"')


def quoted_in_page(before: str, after: str, existing: str) -> list[str]:
    """String literals that the change REMOVES or REPLACES and that the existing page repeats (an error message, a header name, a label): the
    page is wrong after such an edit even though no number, route or shape changed. Empty when there is no page text to compare."""
    if not (existing or "").strip():
        return []
    kept = {l.strip() for l in (after or "").split("\n")}
    page = existing.lower()
    found: list[str] = []
    for line in (before or "").split("\n"):
        if line.strip() in kept:
            continue
        for lit in _STRING.findall(line):
            if lit.lower() in page and lit not in found:
                found.append(lit)
    return found


def prefilter(before: str, after: str, min_diff_lines: int, existing: str = "") -> dict:
    """{proceed, forced, reason, tag?, metrics}. `existing` is the current page text (code mode): used to notice edits to text the page quotes."""
    diff = diff_line_count(before, after)
    structure = structural_change(before, after)
    if diff["total"] < min_diff_lines:
        # A tiny diff is usually noise, but ONE line can change a documented fact (a limit, a default, a status code, a route). When the change
        # carries fact tokens, a public symbol or a shape change it is processed anyway, and forced so the "page already says this" stop cannot
        # swallow it (an embedding barely moves for 50 -> 100). Pure wording of fewer than the minimum lines is still dropped.
        pub_small = diff_public_symbols(before, after)
        if structure["changed"] or pub_small["touched"]:
            what = ", ".join(structure["reasons"] or ["public interface"])
            return {"proceed": True, "forced": True, "reason": f"small diff ({diff['total']} line(s)) that changes documented facts: {what}",
                    "metrics": {"diff": diff, "structure": structure, "smallFactChange": True}}
        quoted = quoted_in_page(before, after, existing)
        if quoted:
            return {"proceed": True, "forced": True, "reason": f"small diff ({diff['total']} line(s)) that edits text the page quotes: {quoted[0]!r}",
                    "metrics": {"diff": diff, "structure": structure, "smallFactChange": True, "quotedText": quoted[:3]}}
        return {"proceed": False, "forced": False, "reason": f"diff of {diff['total']} line(s) is below the {min_diff_lines}-line minimum",
                "tag": "trivial_diff", "metrics": {"diff": diff}}
    # A changed public signature, route, schema field or config key is a real change even when no function was added or removed
    # (the structural check only sees names and counts).
    pub = diff_public_symbols(before, after)
    if pub["touched"] and not structure["changed"]:
        names = pub["names"]
        return {"proceed": True, "forced": True, "reason": f"public interface changed: {', '.join(names[:5])}{', ...' if len(names) > 5 else ''}",
                "metrics": {"diff": diff, "structure": structure, "publicInterface": len(names)}}
    if not structure["changed"]:
        quoted = quoted_in_page(before, after, existing)
        if quoted:
            return {"proceed": True, "forced": True, "reason": f"edits text the page quotes: {quoted[0]!r}",
                    "metrics": {"diff": diff, "structure": structure, "quotedText": quoted[:3]}}
        return {"proceed": False, "forced": False, "reason": "no structural or fact-bearing change (wording only)", "tag": "no_structural_change",
                "metrics": {"diff": diff, "structure": structure}}
    return {
        "proceed": True,
        "forced": structure["forced"],
        "reason": f"shape changed: {', '.join(structure['reasons'])}" if structure["forced"] else f"fact tokens changed: {', '.join(structure['reasons'])}",
        "metrics": {"diff": diff, "structure": structure},
    }
