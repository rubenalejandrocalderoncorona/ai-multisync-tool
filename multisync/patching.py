"""Section-level patching of an existing markdown page. Pure functions, no model call.

  split_sections(text)            -> [{id, level, heading, text}]   concatenating every `text` gives back `text` byte for byte
  apply_operations(sections, ops) -> str                            untouched sections stay byte-identical
  retained_pct(old, new)          -> float                          share of the old non-blank lines still present unchanged
  source_diff(before, after, files) -> str                          what changed in the source, for the patch prompt
  doc_diff(before, after, path)   -> str                            the same for one source markdown document (docs mode)

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


def doc_diff(before: str | None, after: str | None, path: str = "document.md", max_chars: int = 12000) -> str:
    """Unified diff of one source document (docs mode): the source text is what changed, the page is converted from it."""
    old, new = (before or "").splitlines(), (after or "").splitlines()
    text = ""
    for ctx in (3, 1):
        text = "\n".join(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", n=ctx, lineterm=""))
        if len(text) <= max_chars:
            return text
    return text[:max_chars] + "\n[diff truncated]"


# ── scoping a patch ──────────────────────────────────────────────────────────
STOPWORDS = {"private", "public", "protected", "static", "final", "const", "return", "class", "import", "package", "void", "string", "this", "that", "with",
             "from", "true", "false", "null", "else", "elif", "def", "var", "let", "int", "long", "new", "the", "and", "for", "not", "file", "diff", "index"}
WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|\d+")


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _tokens(text: str) -> set[str]:
    out = set()
    for w in WORD.findall(text or ""):
        n = _norm(w)
        if (w.isdigit() and len(w) >= 2) or (not w.isdigit() and len(n) >= 4 and n not in STOPWORDS):
            out.add(n)
    return out


def diff_tokens(diff_text: str, extra=()) -> set[str]:
    """Normalised tokens (lower case, no punctuation) of what the diff added or removed (not its context lines), plus `extra` names (changed
    symbols, changed file names)."""
    lines = [l[1:] for l in (diff_text or "").splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    toks = _tokens("\n".join(lines))
    for e in extra or []:
        base = re.sub(r"\.[A-Za-z0-9]+$", "", str(e).rsplit("/", 1)[-1])
        toks |= {_norm(w) for w in WORD.findall(base) if len(_norm(w)) >= 4} | ({_norm(base)} if len(_norm(base)) >= 4 else set())
    return toks


def _identifier_like(text: str) -> set[str]:
    """What makes a rewrite more than a style change: numbers, code spans, paths and identifiers (snake_case, camelCase, CONSTANT)."""
    out = set(re.findall(r"\d+", text or ""))
    out |= set(re.findall(r"`([^`]+)`", text or ""))
    out |= set(re.findall(r"/[A-Za-z0-9_{}\-./]+", text or ""))
    out |= {w for w in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", text or "") if "_" in w or re.search(r"[a-z][A-Z]", w) or (w.isupper() and len(w) > 2)}
    return out


def drop_unrelated(sections: list[dict], ops, diff_text: str, extra=()) -> tuple[list, list[dict]]:
    """Conservative deterministic guard. A `replace` whose old and new text differ only in style (the new text introduces no number, code span, path or
    identifier) and that has no connection to the change (no diff token in the old text, none introduced by the new text) is dropped: the section stays
    byte-identical. Inserts and deletes are never dropped. Returns (kept ops, [{section, reason}])."""
    toks = diff_tokens(diff_text, extra)
    if not toks:
        return list(ops), []
    by_id = {s["id"]: s for s in sections}
    kept, dropped = [], []
    for op in ops:
        s = by_id.get(op.get("section")) if isinstance(op, dict) else None
        if not s or op.get("op") != "replace":
            kept.append(op)
            continue
        old, new = s["text"], str(op.get("text") or "")
        old_norm, new_norm = _norm(old), _norm(new)
        related = any(t in old_norm for t in toks) or any(t in new_norm and t not in old_norm for t in toks)
        if not related and not (_identifier_like(new) - _identifier_like(old)):
            dropped.append({"section": s["id"], "reason": "patch_unrelated_edit"})
        else:
            kept.append(op)
    return kept, dropped


def scope_of(sections: list[dict], ops) -> dict:
    """The text a patched page is judged on. changedText: the new text of every replaced and inserted section (and a note per deleted one);
    unchangedText: every section the operations left alone (read-only context); changedIds: what the change touches, named for the feedback."""
    ops = _clean_ops(sections, ops)
    replaced = {o["section"] for o in ops if o["op"] == "replace"}
    deleted = {o["section"] for o in ops if o["op"] == "delete"}
    changed = [o["text"].strip("\n") for o in ops if o["op"] in ("replace", "insert_after")]
    changed += [f"(section removed: {sid})" for sid in sorted(deleted)]
    ids = [o["section"] for o in ops if o["op"] in ("replace", "delete")]
    for o in ops:
        if o["op"] == "insert_after":
            m = HEADING.match(o["text"].lstrip("\n").split("\n", 1)[0])
            ids.append(f"new section after {o['section']}" + (f": {m.group(2)}" if m else ""))
    unchanged = "".join(s["text"] for s in sections if s["id"] not in replaced | deleted)
    return {"changedText": "\n\n".join(changed), "unchangedText": unchanged, "changedIds": ids}
