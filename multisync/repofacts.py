"""Facts about a repository as a whole, stored in the FactStore (table repo_facts), with provenance.

Two sources, both grounded:
  deterministic        read from the repository itself with no model: language mix, frameworks, entry points, build targets, CI workflows,
                       HTTP routes, environment variables. verification_method = deterministic.
  llm_quote_grounded   extracted by the cheap model from the README. Every fact must carry a verbatim quote, and a fact whose quote is not
                       found in the source text is dropped.

Every fact records where it came from (source_path), the sha256 of that source when it was extracted (source_hash) and when
(extracted_at). That makes staleness a hash compare: if the README changed since a fact was extracted, the fact is stale. Staleness is
checked wherever the facts are read for the planner or the judge (checked_repo_facts), and stale facts are rebuilt inline before use.
A README-derived fact that contradicts a deterministic one (a Go version, the main language, a framework version) is flagged and shown
to the judge as an explicit conflict.

Aggregate sources: '@source-files' (the measured source files), '@workflows' (.github/workflows), '@entrypoints' (cmd/*/main.go).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from .codesource import select_files
from .prompts import load_prompt
from .symbols import extract_public_symbols

CONFLICT = "contradicts_deterministic_source"

LANGUAGES = {
    "go": "Go", "py": "Python", "ts": "TypeScript", "tsx": "TypeScript", "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript",
    "java": "Java", "kt": "Kotlin", "rb": "Ruby", "rs": "Rust", "swift": "Swift", "c": "C", "h": "C", "cpp": "C++", "cs": "C#", "php": "PHP", "sh": "Shell",
    "sql": "SQL", "prisma": "Prisma schema", "proto": "Protocol Buffers",
}
FRAMEWORKS = {
    "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte", "astro": "Astro", "tailwindcss": "Tailwind CSS", "express": "Express", "fastify": "Fastify",
    "@trpc/server": "tRPC", "prisma": "Prisma", "@prisma/client": "Prisma", "vite": "Vite", "typescript": "TypeScript",
}
GO_NOTABLE = {
    "bubbletea": "Bubble Tea (terminal UI)", "lipgloss": "Lip Gloss (terminal styling)", "go-github": "the go-github client", "gin-gonic/gin": "Gin",
    "labstack/echo": "Echo", "go-chi/chi": "chi", "gorilla/mux": "gorilla/mux", "cobra": "Cobra (CLI)", "gorm": "GORM", "pgx": "pgx (PostgreSQL)", "oauth2": "OAuth2",
}
PY_NOTABLE = {"fastapi": "FastAPI", "flask": "Flask", "django": "Django", "langgraph": "LangGraph", "httpx": "httpx", "psycopg": "psycopg (PostgreSQL)", "pytest": "pytest",
              "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy"}


def _fact(category: str, fact: str, evidence: str, source_path: str, source: str = "deterministic") -> dict:
    return {"category": category, "fact": fact, "evidence": evidence, "source": source, "source_path": source_path,
            "verification_method": "deterministic" if source == "deterministic" else "llm_quote_grounded"}


def _human_list(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SourceReader:
    """Reads the sources of facts at one commit and hashes them. Reads are cached, so checking many facts costs each file once."""

    def __init__(self, git, commit: str):
        self.commit, self._git = commit, git
        self.files = git.list_files(commit)
        self._cache: dict[str, str | None] = {}
        self._digests: dict[str, str] = {}

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            self._cache[path] = self._git.read_at(self.commit, path)
        return self._cache[path]

    def digest(self, source_path: str) -> str:
        """sha256 of the source's content. '' when it no longer exists, which never equals a stored hash."""
        if source_path in self._digests:
            return self._digests[source_path]
        if source_path == "@source-files":
            members = sorted(f for f in select_files(self.files) if "." in f and f.rsplit(".", 1)[-1].lower() in LANGUAGES)
        elif source_path == "@workflows":
            members = sorted(f for f in self.files if f.startswith(".github/workflows/"))
        elif source_path == "@entrypoints":
            members = sorted(f for f in self.files if re.search(r"(^|/)cmd/[^/]+/main\.go$", f))
        else:
            raw = self.read(source_path)
            self._digests[source_path] = sha256(raw) if raw is not None else ""
            return self._digests[source_path]
        h = hashlib.sha256()
        for m in members:
            h.update(f"{m}\0{sha256(self.read(m) or '')}\n".encode())
        self._digests[source_path] = h.hexdigest() if members else ""
        return self._digests[source_path]


def is_stale(fact: dict, reader: SourceReader) -> bool:
    """True when the source changed since the fact was extracted (or the fact predates provenance tracking)."""
    return not fact.get("source_hash") or reader.digest(fact.get("source_path", "")) != fact["source_hash"]


def deterministic_facts(repo: str, files: list[str], read) -> list[dict]:
    """files: every path of the commit; read(path) -> text or None."""
    name = repo.split("/")[-1]
    out: list[dict] = []

    # languages, by bytes of non-test source
    size: dict[str, int] = defaultdict(int)
    count: dict[str, int] = defaultdict(int)
    for f in select_files(files):
        lang = LANGUAGES.get(f.rsplit(".", 1)[-1].lower()) if "." in f else None
        if not lang:
            continue
        raw = read(f)
        if raw is None or "\u0000" in raw:
            continue
        size[lang] += len(raw.encode("utf-8"))
        count[lang] += 1
    total = sum(size.values())
    if total:
        ranked = sorted(size, key=lambda k: -size[k])
        parts = [f"{k} ({round(100 * size[k] / total)}%)" for k in ranked[:4]]
        top = ranked[0]
        main = f"{name} is mainly written in {top}" if size[top] / total >= 0.5 else f"{name} is written in {_human_list([k for k in ranked[:3]])}"
        out.append(_fact("language", f"{main}; source mix: {', '.join(parts)}.", f"{sum(count.values())} source files measured by size (tests, lockfiles and vendored code excluded)", "@source-files"))

    names = set(files)
    # Go
    for gm in [f for f in files if f == "go.mod" or f.endswith("/go.mod")][:2]:
        text = read(gm) or ""
        mod = re.search(r"(?m)^module\s+(\S+)", text)
        ver = re.search(r"(?m)^go\s+(\S+)", text)
        deps = [label for key, label in GO_NOTABLE.items() if key in text]
        fact = f"{gm} defines the Go module {mod.group(1) if mod else '(unnamed)'}" + (f" for Go {ver.group(1)}" if ver else "")
        if deps:
            fact += f", with notable dependencies {_human_list(deps)}"
        out.append(_fact("stack", fact + ".", gm, gm))
    # Node
    for pj in [f for f in files if f.endswith("package.json") and "node_modules" not in f][:3]:
        try:
            data = json.loads(read(pj) or "{}")
        except ValueError:
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        fw = list(dict.fromkeys(label for key, label in FRAMEWORKS.items() if key in deps))
        scripts = list((data.get("scripts") or {}))[:6]
        fact = f"{pj} describes a Node package" + (f" named {data['name']}" if data.get("name") else "")
        if fw:
            fact += f" built with {_human_list(fw)}"
        if scripts:
            fact += f"; npm scripts: {', '.join(scripts)}"
        out.append(_fact("stack", fact + ".", pj, pj))
    # Python
    for pf in [f for f in files if f in ("pyproject.toml", "requirements.txt") or f.endswith("/requirements.txt")][:2]:
        text = (read(pf) or "").lower()
        deps = [label for key, label in PY_NOTABLE.items() if re.search(rf"(?m)(^|[\s\"'\[]){re.escape(key)}", text)]
        if deps:
            out.append(_fact("stack", f"{pf} declares Python dependencies including {_human_list(deps)}.", pf, pf))
    # entry points, build targets, CI, containers
    entries = [f for f in files if re.search(r"(^|/)cmd/[^/]+/main\.go$", f)]
    if entries:
        out.append(_fact("build", f"Go entry points: {', '.join(entries[:6])}.", ", ".join(entries[:6]), "@entrypoints"))
    if "Makefile" in names:
        targets = re.findall(r"(?m)^([A-Za-z][\w-]*):(?!=)", read("Makefile") or "")
        if targets:
            out.append(_fact("build", f"The Makefile defines the targets {_human_list(list(dict.fromkeys(targets))[:8])}.", "Makefile", "Makefile"))
    wf = [f for f in files if f.startswith(".github/workflows/")]
    if wf:
        out.append(_fact("ci", f"GitHub Actions workflows: {', '.join(os.path.basename(w) for w in wf[:6])}.", ", ".join(wf[:6]), "@workflows"))
    for marker, text in (("Dockerfile", "The repository builds a container image (Dockerfile)."), ("docker-compose.yml", "A docker-compose.yml describes a multi-container setup.")):
        found = marker if marker in names else next((f for f in files if f.endswith("/" + marker)), None)
        if found:
            out.append(_fact("build", text, found, found))

    # public interface read from the code
    routes, configs = [], []
    for f in select_files(files):
        raw = read(f)
        if raw is None or len(raw) > 200_000 or "\u0000" in raw:
            continue
        for sym in extract_public_symbols(f, raw):
            if sym["kind"] == "route" and sym["name"] not in routes:
                routes.append(sym["name"])
            elif sym["kind"] == "config" and sym["name"] not in configs:
                configs.append(sym["name"])
    if routes:
        out.append(_fact("api", f"The code registers {len(routes)} HTTP route(s), for example {_human_list(routes[:4])}.", "route registrations found in source files", "@source-files"))
    if configs:
        out.append(_fact("config", f"The code reads {len(configs)} environment variable(s): {', '.join(configs[:8])}{', ...' if len(configs) > 8 else ''}.", "environment variable reads found in source files", "@source-files"))
    return out


# ── deterministic signals, used to cross-check README-derived facts ─────────────
def deterministic_signals(files: list[str], read) -> dict:
    """{'go': '1.24.2', 'frameworks': {'Next.js': '16'}, 'top_language': 'TypeScript', 'mix': {'TypeScript': 54, 'Go': 41}}"""
    sig: dict = {"go": None, "frameworks": {}, "top_language": None, "mix": {}}
    gm = next((f for f in files if f == "go.mod" or f.endswith("/go.mod")), None)
    if gm:
        m = re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)", read(gm) or "")
        sig["go"] = m.group(1) if m else None
    for pj in [f for f in files if f.endswith("package.json") and "node_modules" not in f][:3]:
        try:
            deps = {**(json.loads(read(pj) or "{}").get("dependencies") or {}), **(json.loads(read(pj) or "{}").get("devDependencies") or {})}
        except ValueError:
            continue
        for key, label in FRAMEWORKS.items():
            v = re.search(r"(\d+)", str(deps.get(key, "")))
            if v and key not in ("typescript", "prisma", "@prisma/client", "@trpc/server"):
                sig["frameworks"].setdefault(label, v.group(1))
    size: dict[str, int] = defaultdict(int)
    for f in select_files(files):
        lang = LANGUAGES.get(f.rsplit(".", 1)[-1].lower()) if "." in f else None
        raw = read(f) if lang else None
        if raw is not None and "\u0000" not in raw:
            size[lang] += len(raw.encode("utf-8"))
    total = sum(size.values())
    if total:
        ranked = sorted(size, key=lambda k: -size[k])
        sig["top_language"] = ranked[0]
        sig["mix"] = {k: round(100 * size[k] / total) for k in ranked[:4]}
    return sig


_LANG_NAMES = {v.lower(): v for v in LANGUAGES.values()} | {"golang": "Go", "node": "JavaScript", "node.js": "JavaScript"}
_FW_PATTERN = "|".join(re.escape(x) for x in ("Next.js", "React", "Vue", "Svelte", "Astro", "Tailwind CSS", "Tailwind", "Express", "Vite"))


def conflicts(fact: dict, sig: dict) -> str | None:
    """Why a README-derived fact contradicts a deterministic source, or None. Only categories with a deterministic counterpart are compared:
    the Go version, the main language, and framework major versions."""
    text = f"{fact['fact']} {fact['evidence']}"
    if sig.get("go"):
        for v in re.findall(r"\bGo\s*v?(\d+\.\d+)", text):
            if v != ".".join(sig["go"].split(".")[:2]):
                return f"the README mentions Go {v} but go.mod declares Go {sig['go']}"
    m = re.search(r"(?i)\b(?:mainly|primarily|mostly|predominantly|largely)\s+(?:written\s+in\s+|developed\s+(?:in|using)\s+)?([A-Za-z+#.]+)", text) or \
        re.search(r"(?i)\bwritten\s+(?:entirely\s+|mostly\s+)?in\s+([A-Za-z+#.]+)", text)
    if m and sig.get("top_language"):
        claimed = _LANG_NAMES.get(m.group(1).lower().strip("."))
        if claimed and claimed != sig["top_language"]:
            mix = ", ".join(f"{k} {v}%" for k, v in sig["mix"].items())
            return f"the README says the project is mainly {claimed} but the measured source mix is {mix}"
    for name, ver in re.findall(rf"\b({_FW_PATTERN})\s*v?(\d+)\b", text):
        label = "Tailwind CSS" if name == "Tailwind" else name
        actual = sig.get("frameworks", {}).get(label)
        if actual and actual != ver:
            return f"the README mentions {label} {ver} but package.json depends on {label} {actual}"
    return None


def flag_conflicts(rows: list[dict], sig: dict) -> int:
    n = 0
    for r in rows:
        if r.get("source") != "llm":
            continue
        why = conflicts(r, sig)
        r["flag"], r["flag_detail"] = (CONFLICT, why) if why else (None, None)
        n += 1 if why else 0
    return n


# ── README facts (model), grounded by a verbatim quote ───────────────────────────
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def readme_files(files: list[str]) -> list[str]:
    return [f for f in files if re.match(r"(?i)^readme(\.[a-z]+)?\.md$", f)]


def llm_facts(llm, sections: dict[str, str]) -> tuple[list[dict], int]:
    """Cheap-model extraction from {path: text}. Each kept fact is attributed to the file whose text contains its quote.
    Returns (facts that passed the quote check, number dropped)."""
    source = "\n".join(f"### FILE: {p}\n{t}\n" for p, t in sections.items())
    prompt = load_prompt("repo-facts")
    r = llm.chat_json([{"role": "system", "content": prompt["text"]}, {"role": "user", "content": f"SOURCE:\n{source}"}], tier="cheap")
    kept, dropped = [], 0
    for f in r.get("facts") if isinstance(r.get("facts"), list) else []:
        fact, ev = (f.get("fact") or "").strip(), (f.get("evidence") or "").strip()
        origin = next((p for p, t in sections.items() if _norm(ev) in _norm(t)), None) if fact and ev and len(ev) <= 400 else None
        if origin:
            cat = f.get("category") if f.get("category") in ("purpose", "feature", "architecture", "usage") else "purpose"
            kept.append(_fact(cat, fact, ev, origin, "llm"))
        else:
            dropped += 1
    return kept[:12], dropped


def _stamp(rows: list[dict], reader: SourceReader) -> None:
    now = _now()
    for r in rows:
        r["source_hash"] = reader.digest(r["source_path"])
        r["extracted_at"] = now


def profile_repo(repo: str, commit: str, git, facts, llm=None, only_llm_paths: list[str] | None = None, reader: SourceReader | None = None) -> dict:
    """Refresh the repository's facts. The deterministic ones are rebuilt every time (free). README facts are re-extracted only for READMEs
    whose content changed since their facts were extracted (or all of them when only_llm_paths is None and none are current)."""
    reader = reader or SourceReader(git, commit)
    det = deterministic_facts(repo, reader.files, reader.read)
    _stamp(det, reader)
    facts.replace_repo_facts(repo, "deterministic", det, commit)
    stats = {"deterministic": len(det), "llm": 0, "llmDropped": 0, "llmSkipped": False, "conflicts": 0}

    facts.delete_repo_facts(repo, "llm", [""])  # facts stored before provenance existed cannot be verified; they would only linger as stale duplicates
    stored = facts.repo_facts(repo, "llm")
    if llm is None:
        stats["llmSkipped"] = True
    else:
        candidates = readme_files(reader.files)
        current = {r["source_path"] for r in stored if r["source_hash"] and reader.digest(r["source_path"]) == r["source_hash"]}
        todo = [p for p in (only_llm_paths if only_llm_paths is not None else candidates) if p in candidates and (only_llm_paths is not None or p not in current)]
        if not todo:
            stats["llmSkipped"] = True
        else:
            sections = {p: (reader.read(p) or "")[:9000] for p in todo}
            extracted, dropped = llm_facts(llm, sections)
            _stamp(extracted, reader)
            facts.delete_repo_facts(repo, "llm", todo)
            facts.add_repo_facts(repo, extracted, commit)
            stats.update(llm=len(extracted), llmDropped=dropped)
    # the cross-check runs on every README-derived fact, new or kept, against the fresh deterministic signals
    rows = facts.repo_facts(repo, "llm")
    stats["llm"] = max(stats["llm"], len(rows)) if stats["llmSkipped"] else stats["llm"]
    stats["conflicts"] = flag_conflicts(rows, deterministic_signals(reader.files, reader.read))
    facts.set_repo_fact_flags(repo, rows)
    return stats


def checked_repo_facts(facts, repo: str, commit: str, git, llm=None, reader: SourceReader | None = None) -> tuple[list[dict], dict]:
    """The facts for the planner and the judge, with the staleness check: every fact's source is hashed again; stale facts are rebuilt inline
    before they are used. Without a model the stale README facts cannot be rebuilt, so they are left out instead of trusted."""
    rows = facts.repo_facts(repo)
    report = {"facts": len(rows), "stale": 0, "rebuilt": False, "conflicts": 0, "excluded": 0}
    if not rows:
        return rows, report
    reader = reader or SourceReader(git, commit)
    stale = [r for r in rows if is_stale(r, reader)]
    report["stale"] = len(stale)
    if stale:
        llm_paths = sorted({r["source_path"] for r in stale if r["source"] == "llm"})
        profile_repo(repo, commit, git, facts, llm, only_llm_paths=llm_paths if llm is not None else [], reader=reader)
        rows = facts.repo_facts(repo)
        report["rebuilt"] = True
        left = [r for r in rows if is_stale(r, reader)]
        if left:  # could not be refreshed (no model, or the source is gone): do not treat them as ground truth
            rows = [r for r in rows if r not in left]
            report["excluded"] = len(left)
    report["conflicts"] = sum(1 for r in rows if r.get("flag"))
    report["facts"] = len(rows)
    return rows, report


def facts_text(rows: list[dict]) -> str:
    """REPO_FACTS for the planner. A conflicting README fact is shown as a conflict, never as a fact."""
    return "\n".join(
        f"- CONFLICT, do not state: \"{r['fact']}\" ({r['flag_detail']}). The deterministic source is authoritative." if r.get("flag") else f"- {r['fact']}"
        for r in rows)


def known_fact_lines(rows: list[dict]) -> list[str]:
    """KNOWN_FACTS for the judge: verified facts as they are, conflicts as explicit CONFLICT lines."""
    return [f"CONFLICT: the README-derived fact \"{r['fact']}\" contradicts a deterministic source ({r['flag_detail']}). Treat the deterministic value as true; a page repeating the README claim is wrong."
            if r.get("flag") else r["fact"] for r in rows]
