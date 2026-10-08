"""Layer 1 of the cheap-to-expensive cascade. Returns a verdict without touching the network."""
from __future__ import annotations

import re

from .structure import diff_line_count, structural_change
from .symbols import diff_public_symbols, extract_public_symbols, split_snapshot

_HEADER_LINE = re.compile(r"(?m)^### FILE: .*\n?")


def strip_headers(text: str) -> str:
    """The per-file header lines of a code snapshot are packaging, not content: they never count as a heading, a fact token or a diff line."""
    return _HEADER_LINE.sub("", text or "")


def drop_file_churn(before: str, after: str) -> tuple[str, str, dict]:
    """Remove from both snapshots the files that ENTERED or LEFT the scope and define no public symbol (a data file, notes, a private helper).
    Nothing a reader can depend on changed because of them. Files with public symbols (a route, an exported class, a config key) stay. Only
    applies when there is a previous snapshot: the first documentation of a page has every file entering."""
    info = {"added": 0, "removed": 0}
    if not (before or "").strip():
        return before, after, info
    bb, ab = split_snapshot(before), split_snapshot(after)
    if any(not b["path"] for b in bb + ab):
        return before, after, info
    bp, ap = {b["path"] for b in bb}, {b["path"] for b in ab}

    def keep(blocks, other, kind):
        out = []
        for b in blocks:
            if b["path"] not in other and not extract_public_symbols(b["path"], b["text"]):
                info[kind] += 1
                continue
            out.append(f"### FILE: {b['path']}{b['text']}")
        return "".join(out)

    return keep(bb, ap, "removed"), keep(ab, bp, "added"), info


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
    before, after, churn = drop_file_churn(before, after)
    pub_before, pub_after = before, after  # with headers: public symbols are keyed by file
    before, after = strip_headers(before), strip_headers(after)
    diff = diff_line_count(before, after)
    structure = structural_change(before, after)
    if (churn["added"] or churn["removed"]) and diff["total"] == 0 and not diff_public_symbols(pub_before, pub_after)["touched"]:
        return {"proceed": False, "forced": False, "tag": "no_documented_change",
                "reason": f"only files without public symbols entered or left the scope ({churn['added']} added, {churn['removed']} removed): nothing to document",
                "metrics": {"diff": diff, "fileChurn": churn}}
    if diff["total"] < min_diff_lines:
        # A tiny diff is usually noise, but ONE line can change a documented fact (a limit, a default, a status code, a route). When the change
        # carries fact tokens, a public symbol or a shape change it is processed anyway, and forced so the "page already says this" stop cannot
        # swallow it (an embedding barely moves for 50 -> 100). Pure wording of fewer than the minimum lines is still dropped.
        pub_small = diff_public_symbols(pub_before, pub_after)
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
    pub = diff_public_symbols(pub_before, pub_after)
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
