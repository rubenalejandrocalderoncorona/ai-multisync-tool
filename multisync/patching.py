"""Section-level patching of an existing markdown page. Pure functions, no model call.

  split_sections(text)            -> [{id, level, heading, text}]   concatenating every `text` gives back `text` byte for byte
  apply_operations(sections, ops) -> str                            untouched sections stay byte-identical
  retained_pct(old, new)          -> float                          share of the old non-blank lines still present unchanged
  source_diff(before, after, files) -> str                          what changed in the source, for the patch prompt

An operation is {"op": "replace", "section": id, "text": ...}, {"op": "insert_after", "section": id, "text": ...} or
{"op": "delete", "section": id}. Anything malformed or pointing at an unknown section raises PatchError: the caller falls back
to a full rewrite instead of guessing.
"""
from __future__ import annotations

import difflib
import re

PREAMBLE_ID = "(preamble)"
HEADING = re.compile(r"^ {0,3}(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CHANGE_HISTORY = re.compile(r"(?i)\n?#{2,3}\s+Change History\b[\s\S]*?(?=\n#{2,3}\s+(?!Change History)\S|\Z)")
OPS = ("replace", "insert_after", "delete")


class PatchError(ValueError):
    """The model's operations cannot be applied safely."""


def strip_generated(text: str) -> str:
    """The parts of a published page that code, not the model, owns: front matter and the Change History table."""
    body = re.sub(r"^---\n[\s\S]*?\n---\n+", "", text or "", count=1)
    return CHANGE_HISTORY.sub("", body).rstrip("\n") + "\n" if body.strip() else ""


def split_sections(text: str) -> list[dict]:
    """Split on markdown headings of level 1-4; a `#` inside a fenced code block is not a heading. Text before the first heading is
    the preamble section. Ids are heading paths ("Endpoints > Listings > GET /api"); a repeated path gets " #2", " #3"."""
    sections: list[dict] = []
    stack: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    cur: dict = {"level": 0, "heading": "", "path": PREAMBLE_ID, "lines": []}

    def close():
        if cur["lines"]:
            sections.append({"level": cur["level"], "heading": cur["heading"], "path": cur["path"], "text": "".join(cur["lines"])})

    for line in (text or "").splitlines(keepends=True):
        f = FENCE.match(line)
        if f:
            ch, n = f.group(1)[0], len(f.group(1))
            if fence is None:
                fence = (ch, n)
            elif fence[0] == ch and n >= fence[1] and not line.strip(" \t\r\n").strip(ch):
                fence = None
        h = None if fence is not None or f else HEADING.match(line.rstrip("\r\n"))
        if h:
            close()
            level, title = len(h.group(1)), h.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur = {"level": level, "heading": title, "path": " > ".join(t for _, t in stack), "lines": [line]}
        else:
            cur["lines"].append(line)
    close()
    seen: dict[str, int] = {}
    for s in sections:
        n = seen[s["path"]] = seen.get(s["path"], 0) + 1
        s["id"] = s.pop("path") + (f" #{n}" if n > 1 else "")
    return sections


def _clean_ops(sections: list[dict], ops) -> list[dict]:
    if not isinstance(ops, list):
        raise PatchError("operations must be a list")
    ids = {s["id"] for s in sections}
    out, touched = [], set()
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise PatchError(f"operation {i}: unknown op {op.get('op') if isinstance(op, dict) else op!r}")
        sid = op.get("section")
        if sid not in ids:
            raise PatchError(f"operation {i}: unknown section id {sid!r}")
        if op["op"] != "insert_after":
            if sid in touched:
                raise PatchError(f"operation {i}: section {sid!r} is changed more than once")
            touched.add(sid)
        if op["op"] != "delete":
            text = op.get("text")
            if not isinstance(text, str) or not text.strip():
                raise PatchError(f"operation {i}: {op['op']} needs a non-empty text")
            if op["op"] == "replace" and sid != PREAMBLE_ID and not HEADING.match(text.lstrip("\n").split("\n", 1)[0]):
                raise PatchError(f"operation {i}: replacement for {sid!r} must start with its heading line")
        out.append(op)
    return out


def changed_ids(sections: list[dict], ops) -> dict:
    """{replaced, deleted, inserted}: the section ids each kind of operation touches (inserted = anchors)."""
    ops = _clean_ops(sections, ops)
    return {k: [o["section"] for o in ops if o["op"] == n] for k, n in (("replaced", "replace"), ("deleted", "delete"), ("inserted", "insert_after"))}


def apply_operations(sections: list[dict], ops) -> str:
    ops = _clean_ops(sections, ops)
    replace = {o["section"]: o["text"] for o in ops if o["op"] == "replace"}
    delete = {o["section"] for o in ops if o["op"] == "delete"}
    after: dict[str, list[str]] = {}
    for o in ops:
        if o["op"] == "insert_after":
            after.setdefault(o["section"], []).append(o["text"])
    out: list[str] = []
    for s in sections:
        sid, orig = s["id"], s["text"]
        gap = len(orig) - len(orig.rstrip("\n"))  # the blank lines that separate this section from the next
        if sid in delete:
            pass
        elif sid in replace:
            out.append(replace[sid].lstrip("\n").rstrip("\n") + "\n" * max(gap, 1))
        else:
            out.append(orig)
        for t in after.get(sid, []):
            if out and not out[-1].endswith("\n\n"):
                out[-1] = out[-1] if out[-1].endswith("\n") else out[-1] + "\n"
                out.append("\n")
            out.append(t.strip("\n") + "\n" + ("\n" if gap > 1 else ""))
    return "".join(out)


def retained_pct(old: str, new: str) -> float:
    """Share (0-100) of the old page's non-blank lines that survive byte-identical in the new page (multiset, order-insensitive)."""
    bag: dict[str, int] = {}
    old_lines = [l for l in (old or "").split("\n") if l.strip()]
    for l in (new or "").split("\n"):
        if l.strip():
            bag[l] = bag.get(l, 0) + 1
    kept = 0
    for l in old_lines:
        if bag.get(l, 0) > 0:
            bag[l] -= 1
            kept += 1
    return round(100.0 * kept / len(old_lines), 1) if old_lines else 100.0


def _by_file(snapshot: str | None) -> dict[str, str]:
    parts = re.split(r"(?m)^### FILE: (.+)\n", snapshot or "")
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def source_diff(before: str | None, after: str | None, changed_files: list[str] | None = None, max_chars: int = 12000) -> str:
    """Unified diff of the source snapshots, limited to the changed files. Small: the full diff. Large: only the changed lines with one
    line of context, per file, cut at `max_chars`."""
    a, b = _by_file(before), _by_file(after)
    names = [re.sub(r"\s*\(removed\)$", "", f) for f in (changed_files or [])]
    names = [n for n in names if n in a or n in b] or sorted(set(a) | set(b))
    full, short = [], []
    for n in names:
        old, new = a.get(n, "").splitlines(), b.get(n, "").splitlines()
        for ctx, bucket in ((3, full), (1, short)):
            d = list(difflib.unified_diff(old, new, f"a/{n}", f"b/{n}", n=ctx, lineterm=""))
            if d:
                bucket.append("\n".join(d))
    text = "\n".join(full)
    if len(text) <= max_chars:
        return text
    return "\n".join(short)[:max_chars] + "\n[diff truncated]"
