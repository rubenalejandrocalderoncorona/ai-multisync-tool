"""Code-driven mode: turn a commit's code changes into "change units", one per documentation page the repo declares
(config/repos.json -> repos[repo].pages). Pure functions; git access is injected so the logic is testable without a repository.

A page declares which files document it:
  { "path": "overview.md", "kind": "README / project overview", "scope": ["**"], "exclude": [] }
  { "path": "api.md",      "kind": "API documentation",         "scope": ["src/routes/**", "openapi.yaml"] }
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable

DEFAULT_EXCLUDE = [
    "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**", "**/.next/**", "**/.astro/**", "**/coverage/**",
    "**/__pycache__/**", "**/*.min.js", "**/*.map", "**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/go.sum", "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.svg", "**/*.ico", "**/*.woff*", "**/*.pdf", "**/*.zip",
    "**/*.test.*", "**/*.spec.*", "**/test/**", "**/tests/**", "**/__tests__/**", "**/*_test.go",
    ".github/**", "docs/**", "documentation/**", "**/*.snap", "**/.env*", "**/*.pem", "**/*.key",
]
# ONE definition of "looks sensitive", shared by the onboarding guard (multisync/cli/onboard_check.py) and by change detection below.
# Classification markers in document-type files (notes, exports, fixtures). Ordinary code that uses the word "restricted" is not flagged.
SENSITIVE_CONTENT = re.compile(r"(classification\s*:[^\n]*\b(restricted|confidential|secret)\b|internal use only|do not distribute|strictly confidential|confidential and proprietary)", re.I)
DOCLIKE = re.compile(r"\.(md|mdx|txt|csv|json|ya?ml)$", re.I)
# Secret-type FILES and folders, not ordinary code that merely mentions credentials (features/credentials/data.ts is fine).
SENSITIVE_PATH = re.compile(r"(^|/)(\.env(\..+)?$|[^/]*\.(pem|key|p12|pfx|keystore)$|id_(rsa|ed25519|ecdsa)$|credentials\.(json|ya?ml)$|service[-_]?account[^/]*\.json$|secrets?\.(json|ya?ml)$|secrets?/)", re.I)
_PATHLIKE = re.compile(r"[\w.@+-]+(?:/[\w.@+-]+)+/?|[\w.@+-]+\.\w{1,8}\b")
SECRET_LINE = re.compile(r"(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{16,}", re.I)


@lru_cache(maxsize=2048)
def glob_to_regexp(glob: str) -> re.Pattern:
    """Minimal glob: ** any depth, * within a segment, ? one char."""
    out = ""
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "*":
            if glob[i + 1:i + 2] == "*":
                if glob[i + 2:i + 3] == "/":
                    out += "(?:.*/)?"
                    i += 2
                else:
                    out += ".*"
                    i += 1
            else:
                out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(f"^{out}$")


def matches_any(file: str, globs: list[str]) -> bool:
    return any(glob_to_regexp(g).match(file) for g in globs)


def select_files(files: list[str], scope: list[str] | None = None, exclude: list[str] | None = None) -> list[str]:
    scope = scope if scope else ["**"]
    excl = [*DEFAULT_EXCLUDE, *(exclude or [])]
    return [f for f in files if matches_any(f, scope) and not matches_any(f, excl)]


def is_sensitive(path: str, content: str | None = None) -> bool:
    """A secret-type path, or (document-type files, when the content is given) a restricted/confidential marker."""
    if SENSITIVE_PATH.search(path or ""):
        return True
    return bool(content and DOCLIKE.search(path or "") and SENSITIVE_CONTENT.search(content))


def redact_sensitive_paths(text: str) -> str:
    """Replace every token of `text` that is a secret-type path with a placeholder, for strings that may become public (PR body, ticket)."""
    return _PATHLIKE.sub(lambda m: "[sensitive file]" if SENSITIVE_PATH.search(m.group(0)) else m.group(0), text or "")


def scrub(text: str) -> str:
    """Drop lines that look like embedded secrets so they never reach a model or a log."""
    return "\n".join("[line removed: looks like a secret]" if SECRET_LINE.search(l) else l for l in text.split("\n"))


def snapshot(read_file: Callable[[str], str | None], files: list[str], changed: list[str] | None = None, max_chars: int = 60000,
             max_file_chars: int = 12000) -> dict:
    """Concatenate files into one snapshot. Changed files come first so they survive the size cap. Binary-looking content is
    skipped; each file and the whole snapshot are capped."""
    changed = changed or []
    order = [f for f in files if f in changed] + [f for f in files if f not in changed]
    out = ""
    included: list[str] = []
    for f in order:
        raw = read_file(f)
        if raw is None or "\u0000" in raw:
            continue
        body = scrub(f"{raw[:max_file_chars]}\n[truncated]" if len(raw) > max_file_chars else raw)
        block = f"### FILE: {f}\n{body}\n\n"
        if len(out) + len(block) > max_chars:
            break
        out += block
        included.append(f)
    return {"text": out, "files": included}


def pages_scope(pages: list[dict] | None = None) -> list[str]:
    """Union of what the declared pages are allowed to read; no pages (or a page without scope) means everything."""
    pages = pages or []
    if pages and all(p.get("scope") for p in pages):
        return list(dict.fromkeys(s for p in pages for s in p["scope"]))
    return ["**"]


def build_code_changes(*, repo, policy, commit, before, list_files, read_at, changed_between, read_existing_page, full=False) -> list[dict]:
    """Change units (kind 'code'); pages whose files did not change are omitted.

    list_files(rev) -> [path]; read_at(rev, file) -> str|None; changed_between(a, b) -> [path]; read_existing_page(page) -> str."""
    if full:
        before = ""  # a forced full sync treats every page as new, so the prefilter cannot call it "no change"
    pages = policy.get("pages") or [{"path": "overview.md", "kind": policy.get("style")}]
    after_files = list_files(commit)
    before_files = list_files(before) if before else []
    changed_all = after_files if (full or not before) else changed_between(before, commit)
    changed_set = set(changed_all)
    after_set = set(after_files)
    before_set = set(before_files)
    allowed = set(policy.get("allowSensitive") or [])
    flagged: dict[str, bool] = {}

    def sensitive(f: str) -> bool:
        """Path-based for every file; content-based (restricted/confidential marker) for files this commit touches, read on BOTH sides so a file
        that only LOST its marker is still recognised. Such files are invisible to change detection."""
        if f not in flagged:
            hit = f not in allowed and bool(SENSITIVE_PATH.search(f))
            if not hit and f not in allowed and DOCLIKE.search(f) and (f in changed_set or f not in after_set):
                hit = any(t and SENSITIVE_CONTENT.search(t) for t in (read_at(commit, f) if f in after_set else None, read_at(before, f) if before and f in before_set else None))
            flagged[f] = hit
        return flagged[f]

    ignored: set[str] = set()
    units = []
    for page in pages:
        excl = [*(page.get("exclude") or []), *(policy.get("exclude") or [])]
        scoped = select_files(after_files, page.get("scope"), excl)
        before_scoped = select_files(before_files, page.get("scope"), excl)
        ignored |= {f for f in {*scoped, *before_scoped} if sensitive(f) and (f in changed_set or f not in after_set or f not in before_set)}
        scoped = [f for f in scoped if not sensitive(f)]
        before_scoped = [f for f in before_scoped if not sensitive(f)]
        changed = [f for f in scoped if f in changed_set]
        removed = [f for f in before_scoped if f not in after_set and f in changed_set]
        if not changed and not removed:
            continue
        after = snapshot(lambda f: read_at(commit, f), scoped, changed=changed)
        prev = snapshot(lambda f: read_at(before, f), before_scoped, changed=changed + removed) if before else {"text": ""}
        units.append({
            "kind": "code", "repo": repo, "filePath": page["path"], "styleKey": page.get("kind") or policy.get("style") or None, "commit": commit,
            "before": prev["text"] or None, "after": after["text"], "existing": read_existing_page(page["path"]) or "",
            "brief": page.get("brief") or "", "title": page.get("title") or "", "repoMap": scoped[:400], "snapshotFiles": after["files"],
            "changedFiles": [*changed, *[f"{f} (removed)" for f in removed]],
            # Files that entered or left the scope without carrying a public symbol: nothing a reader depends on, so no documentation obligation.
            "noObligationFiles": [f for f in [*changed, *removed] if (f in before_set) != (f in after_set) and f in set(scoped) | set(before_scoped)
                                  and not _has_public_symbols(f, read_at(commit, f) if f in after_set else read_at(before, f))],
        })
    if ignored:
        print(f"{len(ignored)} sensitive-looking file(s) ignored by change detection")
    return units


def _has_public_symbols(path: str, text: str | None) -> bool:
    from .symbols import extract_public_symbols  # local import: symbols does not depend on this module, kept lazy for import order
    return bool(text) and bool(extract_public_symbols(path, text))
