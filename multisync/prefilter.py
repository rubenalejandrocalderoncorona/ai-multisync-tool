"""Layer 1 of the cheap-to-expensive cascade. Returns a verdict without touching the network."""
from __future__ import annotations

from .structure import diff_line_count, structural_change
from .symbols import diff_public_symbols


def prefilter(before: str, after: str, min_diff_lines: int) -> dict:
    """{proceed, forced, reason, tag?, metrics}"""
    diff = diff_line_count(before, after)
    if diff["total"] < min_diff_lines:
        return {"proceed": False, "forced": False, "reason": f"diff of {diff['total']} line(s) is below the {min_diff_lines}-line minimum",
                "tag": "trivial_diff", "metrics": {"diff": diff}}
    structure = structural_change(before, after)
    # A changed public signature, route, schema field or config key is a real change even when no function was added or removed
    # (the structural check only sees names and counts).
    pub = diff_public_symbols(before, after)
    if pub["touched"] and not structure["changed"]:
        names = pub["names"]
        return {"proceed": True, "forced": True, "reason": f"public interface changed: {', '.join(names[:5])}{', ...' if len(names) > 5 else ''}",
                "metrics": {"diff": diff, "structure": structure, "publicInterface": len(names)}}
    if not structure["changed"]:
        return {"proceed": False, "forced": False, "reason": "no structural or fact-bearing change (wording only)", "tag": "no_structural_change",
                "metrics": {"diff": diff, "structure": structure}}
    return {
        "proceed": True,
        "forced": structure["forced"],
        "reason": f"shape changed: {', '.join(structure['reasons'])}" if structure["forced"] else f"fact tokens changed: {', '.join(structure['reasons'])}",
        "metrics": {"diff": diff, "structure": structure},
    }
