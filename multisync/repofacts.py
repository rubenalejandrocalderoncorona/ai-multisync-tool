"""Facts about a repository as a whole, stored in the FactStore (table repo_facts).

Two sources, both grounded:
  deterministic  read from the repository itself with no model: language mix, frameworks, entry points, build targets, CI workflows,
                 HTTP routes, environment variables.
  llm            extracted by the cheap model from the README and manifests. Every fact must carry a verbatim quote, and a fact whose
                 quote is not found in the source text is dropped. The model step is skipped when its input has not changed.
The facts feed the planner and the judge as known context, and answer "what is this project" without reading the code again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict

from .codesource import select_files
from .prompts import load_prompt
from .symbols import extract_public_symbols

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


def _fact(category: str, fact: str, evidence: str, source: str = "deterministic") -> dict:
    return {"category": category, "fact": fact, "evidence": evidence, "source": source}


def _human_list(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


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
        out.append(_fact("language", f"{main}; source mix: {', '.join(parts)}.", f"{sum(count.values())} source files measured by size (tests, lockfiles and vendored code excluded)"))

    names = set(files)
    # Go
    if "go.mod" in names or any(f.endswith("/go.mod") for f in files):
        for gm in [f for f in files if f == "go.mod" or f.endswith("/go.mod")][:2]:
            text = read(gm) or ""
            mod = re.search(r"(?m)^module\s+(\S+)", text)
            ver = re.search(r"(?m)^go\s+(\S+)", text)
            deps = [label for key, label in GO_NOTABLE.items() if key in text]
            fact = f"{gm} defines the Go module {mod.group(1) if mod else '(unnamed)'}" + (f" for Go {ver.group(1)}" if ver else "")
            if deps:
                fact += f", with notable dependencies {_human_list(deps)}"
            out.append(_fact("stack", fact + ".", gm))
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
        out.append(_fact("stack", fact + ".", pj))
    # Python
    for pf in [f for f in files if f in ("pyproject.toml", "requirements.txt") or f.endswith("/requirements.txt")][:2]:
        text = (read(pf) or "").lower()
        deps = [label for key, label in PY_NOTABLE.items() if re.search(rf"(?m)(^|[\s\"'\[]){re.escape(key)}", text)]
        if deps:
            out.append(_fact("stack", f"{pf} declares Python dependencies including {_human_list(deps)}.", pf))
    # entry points, build targets, CI, containers
    entries = [f for f in files if re.search(r"(^|/)cmd/[^/]+/main\.go$", f)]
    if entries:
        out.append(_fact("build", f"Go entry points: {', '.join(entries[:6])}.", ", ".join(entries[:6])))
    if "Makefile" in names:
        targets = re.findall(r"(?m)^([A-Za-z][\w-]*):(?!=)", read("Makefile") or "")
        if targets:
            out.append(_fact("build", f"The Makefile defines the targets {_human_list(list(dict.fromkeys(targets))[:8])}.", "Makefile"))
    wf = [f for f in files if f.startswith(".github/workflows/")]
    if wf:
        out.append(_fact("ci", f"GitHub Actions workflows: {', '.join(os.path.basename(w) for w in wf[:6])}.", ", ".join(wf[:6])))
    for marker, text in (("Dockerfile", "The repository builds a container image (Dockerfile)."), ("docker-compose.yml", "A docker-compose.yml describes a multi-container setup.")):
        if marker in names or any(f.endswith("/" + marker) for f in files):
            out.append(_fact("build", text, marker))

    # public interface read from the code
    routes, configs = [], []
    for f in select_files(files):
        raw = read(f)
        if raw is None or len(raw) > 200_000 or "\u0000" in raw:
            continue
        for s in extract_public_symbols(f, raw):
            if s["kind"] == "route" and s["name"] not in routes:
                routes.append(s["name"])
            elif s["kind"] == "config" and s["name"] not in configs:
                configs.append(s["name"])
    if routes:
        out.append(_fact("api", f"The code registers {len(routes)} HTTP route(s), for example {_human_list(routes[:4])}.", "route registrations found in source files"))
    if configs:
        out.append(_fact("config", f"The code reads {len(configs)} environment variable(s): {', '.join(configs[:8])}{', ...' if len(configs) > 8 else ''}.", "environment variable reads found in source files"))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def llm_source(files: list[str], read) -> str:
    """The text the model sees: README first, then manifests, bounded."""
    parts, budget = [], 9000
    picks = [f for f in files if re.match(r"(?i)^readme(\.[a-z]+)?\.md$", f)] + [f for f in files if f in ("go.mod", "package.json", "pyproject.toml") or f.endswith(("/package.json", "/go.mod"))][:3]
    for f in picks:
        raw = read(f)
        if raw is None:
            continue
        chunk = raw[: min(len(raw), budget)]
        parts.append(f"### FILE: {f}\n{chunk}\n")
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n".join(parts)


def llm_facts(llm, source: str) -> tuple[list[dict], int]:
    """Cheap-model extraction. Returns (facts that passed the quote check, number dropped)."""
    prompt = load_prompt("repo-facts")
    r = llm.chat_json([{"role": "system", "content": prompt["text"]}, {"role": "user", "content": f"SOURCE:\n{source}"}], tier="cheap")
    hay = _norm(source)
    kept, dropped = [], 0
    for f in r.get("facts") if isinstance(r.get("facts"), list) else []:
        fact, ev = (f.get("fact") or "").strip(), (f.get("evidence") or "").strip()
        if fact and ev and len(ev) <= 400 and _norm(ev) in hay:
            kept.append(_fact(f.get("category") if f.get("category") in ("purpose", "feature", "architecture", "usage") else "purpose", fact, ev, "llm"))
        else:
            dropped += 1
    return kept[:12], dropped


def profile_repo(repo: str, commit: str, git, facts, llm=None) -> dict:
    """Refresh the repository's facts. The deterministic ones are rebuilt every time (free); the model step only runs when the README or
    manifests changed since the stored version."""
    files = git.list_files(commit)
    reader = lambda f: git.read_at(commit, f)  # noqa: E731
    det = deterministic_facts(repo, files, reader)
    facts.replace_repo_facts(repo, "deterministic", det, commit, None)
    stats = {"deterministic": len(det), "llm": 0, "llmDropped": 0, "llmSkipped": False}
    if llm is None:
        stats["llmSkipped"] = True
        return stats
    source = llm_source(files, reader)
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    if not source or any(f.get("source_hash") == digest for f in facts.repo_facts(repo, "llm")):
        stats["llmSkipped"] = True
        stats["llm"] = len(facts.repo_facts(repo, "llm"))
        return stats
    extracted, dropped = llm_facts(llm, source)
    facts.replace_repo_facts(repo, "llm", extracted, commit, digest)
    stats.update(llm=len(extracted), llmDropped=dropped)
    return stats


def facts_text(rows: list[dict]) -> str:
    return "\n".join(f"- {r['fact']}" for r in rows)
