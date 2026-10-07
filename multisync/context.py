"""Context stage: make sure the vector database holds the WHOLE repository context before any change is analysed, and retrieve from
it afterwards.

Two collections, two kinds of context:
  code context     (code collection)  every in-scope source file, chunked. "What the system does."
  semantic context (docs collection)  approved pages, the repo's own docs/README (kind source_doc), and page briefs. "What the docs
                                      should say and how."

Indexing is incremental per commit. The first run, a forced run, or any run where the index is not exactly at the previous commit
(a missed run, a rebuilt DB) re-indexes the whole repo, so the index heals itself instead of drifting.
"""
from __future__ import annotations

import hashlib
import re
import time

from .chunker import chunk_code, chunk_markdown, point_id
from .codesource import matches_any, scrub, select_files
from .symbols import doc_tokens, extract_public_symbols

BATCH = 64
MAX_FILE_BYTES = 200_000
DOC_FILE = re.compile(r"^(README(\.[a-z]+)?\.md|(docs|documentation)/.*\.(md|mdx))$", re.I)
LANG = {"js": "javascript", "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
        "py": "python", "go": "go", "java": "java", "rb": "ruby", "rs": "rust", "yaml": "yaml", "yml": "yaml", "json": "json",
        "sh": "shell", "sql": "sql", "md": "markdown"}
MIN_SYMBOL_LEN = 4  # names too generic to count as a reference when found in prose


def lang_of(f: str) -> str:
    return LANG.get((f.rsplit(".", 1)[-1] if "." in f else "").lower(), "text")


def sig_hash(sig: str) -> str:
    return hashlib.sha1((sig or "").encode("utf-8")).hexdigest()[:12]


def record_doc_refs(facts, doc_repo: str, doc_path: str, text: str, kind: str, known: set[str]) -> int:
    """Record which known public symbols a document mentions, so a later change to one of them is seen as "documented elsewhere"."""
    if not hasattr(facts, "replace_doc_refs"):
        return 0
    tokens = doc_tokens(text)
    hits = [n for n in known if len(n) >= MIN_SYMBOL_LEN and n in tokens]
    facts.replace_doc_refs(doc_repo, doc_path, hits, kind)
    return len(hits)


def embed_all(llm, texts: list[str]) -> list:
    out: list = []
    for i in range(0, len(texts), BATCH):
        out.extend(llm.embed(texts[i:i + BATCH]))
    return out


def _binary_or_big(raw: str | None) -> bool:
    return raw is None or "\u0000" in raw or len(raw.encode("utf-8")) > MAX_FILE_BYTES


def sync_context(*, repo, commit, git, code_store, doc_store, llm, facts, before="", full=False, pages=None, scope=None, exclude=None,
                 docs=None, limits=None) -> dict:
    """git: object with list_files(rev), read_at(rev, file), changed_between(a, b)."""
    started = time.time()
    pages, scope, exclude, docs, limits = pages or [], scope or ["**"], exclude or [], docs or [], limits or {}
    max_files = limits.get("maxFiles", 3000)
    max_chunks = limits.get("maxChunks", 8000)

    all_files = git.list_files(commit)
    # The index may only contain what the repo's declared pages are allowed to read.
    allowed = set(select_files(all_files, scope, exclude))
    code_files = [f for f in all_files if f in allowed and not DOC_FILE.match(f)]
    # Semantic sources are not limited to the pages' code scope: the README, docs/ and any `docs` globs the repo declares (for
    # example an existing end-user docs app). The repo-level exclude still applies.
    doc_files = [f for f in all_files if (DOC_FILE.match(f) or matches_any(f, docs)) and not matches_any(f, exclude)]

    state = facts.get_context_state(repo)
    incremental = bool(not full and before and state and state["commit"] == before)
    changed = set(git.changed_between(before, commit)) if incremental else None

    if not incremental:
        code_store.delete_by_repo(repo)
        doc_store.delete_by_repo(repo, "source_doc")
        doc_store.delete_by_repo(repo, "brief")

    symbol_rows: list[dict] = []
    removed = 0
    gone_for_symbols: list[str] = []
    if incremental:
        present = set(all_files)
        gone = [f for f in changed if f not in present]
        # Only paths that could have been indexed count (tests, lockfiles and the like never were).
        for f in [*select_files(gone, scope, exclude), *[g for g in gone if DOC_FILE.match(g)]]:
            code_store.delete_by_path(repo, f)
            doc_store.delete_by_path(repo, f)
            removed += 1
            gone_for_symbols.append(f)

    def wanted(f: str) -> bool:
        return not incremental or f in changed

    to_index = [f for f in code_files if wanted(f)][:max_files]
    code_points: list[dict] = []
    skipped: list[str] = []
    for f in to_index:
        raw = git.read_at(commit, f)
        if _binary_or_big(raw):
            skipped.append(f)
            continue
        if incremental:
            code_store.delete_by_path(repo, f)
        for sy in extract_public_symbols(f, raw, calls=True):
            symbol_rows.append({"path": f, "kind": sy["kind"], "name": sy["name"], "sig_hash": sig_hash(sy["sig"])})
        for i, c in enumerate(chunk_code(f, scrub(raw))):
            if len(code_points) >= max_chunks:
                break
            code_points.append({"id": point_id(repo, f, i), "text": c["text"],
                                "payload": {"repo": repo, "path": f, "start": c["start"], "end": c["end"], "commit": commit, "lang": lang_of(f), "kind": "code", "text": c["text"]}})
    code_vecs = embed_all(llm, [p["text"] for p in code_points])
    code_store.upsert([{"id": p["id"], "vector": code_vecs[i], "payload": p["payload"]} for i, p in enumerate(code_points)])
    if hasattr(facts, "replace_symbols"):
        facts.replace_symbols(repo, symbol_rows, paths=list(dict.fromkeys([*to_index, *gone_for_symbols])) if incremental else None)

    # Semantic side: the repo's own docs/README and the declared page briefs.
    doc_points: list[dict] = []
    for f in [d for d in doc_files if wanted(d)]:
        raw = git.read_at(commit, f)
        if raw is None:
            continue
        if incremental:
            doc_store.delete_by_path(repo, f)
        for i, c in enumerate(chunk_markdown(raw)):
            doc_points.append({"id": point_id(repo, f"src:{f}", i), "text": f"{c['heading']}\n{c['text']}",
                               "payload": {"repo": repo, "path": f, "chunk": i, "heading": c["heading"], "text": c["text"], "commit": commit, "kind": "source_doc"}})
    for pg in [x for x in pages if x.get("brief")]:
        doc_points.append({"id": point_id(repo, f"brief:{pg['path']}", 0), "text": f"{pg['path']}\n{pg['brief']}",
                           "payload": {"repo": repo, "path": f"brief:{pg['path']}", "chunk": 0, "heading": f"Brief: {pg['path']}", "text": pg["brief"], "commit": commit, "kind": "brief"}})
    doc_vecs = embed_all(llm, [p["text"] for p in doc_points])
    doc_store.upsert([{"id": p["id"], "vector": doc_vecs[i], "payload": p["payload"]} for i, p in enumerate(doc_points)])

    doc_refs = 0
    if hasattr(facts, "known_symbol_names"):
        known = facts.known_symbol_names()
        for f in [d for d in doc_files if wanted(d)]:
            raw = git.read_at(commit, f)
            if raw is not None:
                doc_refs += record_doc_refs(facts, repo, f, raw, "source_doc", known)
    total_chunks = code_store.count(repo)
    facts.set_context_state(repo, commit, len(code_files), total_chunks)

    if incremental:
        reason = f"index was at {before[:7]}"
    elif full:
        reason = "forced"
    elif not before:
        reason = "no previous commit"
    elif not state:
        reason = "first run for this repo"
    else:
        reason = f"index at {state['commit'][:7]} but previous commit is {before[:7]}"
    return {
        "mode": "incremental" if incremental else "full", "reason": reason, "repoFiles": len(code_files),
        "filesIndexed": len(to_index) - len(skipped), "filesSkipped": len(skipped), "removed": removed,
        "codeChunks": len(code_points), "docChunks": len(doc_points), "symbols": len(symbol_rows), "docRefs": doc_refs,
        "ms": int((time.time() - started) * 1000),
    }


def sync_site(*, site_repo, commit, files, read_file, doc_store, llm, facts) -> dict:
    """Index the pages of the central documentation site itself (kind site_doc) under the key `site:<repo>`. They are the semantic
    context for terminology, structure and what is already covered elsewhere. Re-indexed only when the site's commit changes."""
    key = f"site:{site_repo}"
    state = facts.get_context_state(key)
    if state and state["commit"] == commit:
        return {"skipped": True, "pages": state["files"], "chunks": state["chunks"], "reason": f"site already indexed at {commit[:7]}"}
    doc_store.delete_by_repo(key)
    pts: list[dict] = []
    known = facts.known_symbol_names() if hasattr(facts, "known_symbol_names") else set()
    refs = 0
    for f in files:
        raw = read_file(f)
        if raw is None:
            continue
        refs += record_doc_refs(facts, key, f, raw, "site_doc", known)
        for i, c in enumerate(chunk_markdown(raw)):
            pts.append({"id": point_id(key, f, i), "text": f"{c['heading']}\n{c['text']}",
                        "payload": {"repo": key, "path": f, "chunk": i, "heading": c["heading"], "text": c["text"], "commit": commit, "kind": "site_doc"}})
    vecs = embed_all(llm, [p["text"] for p in pts])
    doc_store.upsert([{"id": p["id"], "vector": vecs[i], "payload": p["payload"]} for i, p in enumerate(pts)])
    facts.set_context_state(key, commit, len(files), len(pts))
    return {"skipped": False, "pages": len(files), "chunks": len(pts), "docRefs": refs}


def backfill_coupling(*, repo, commit, git, facts, scope=None, exclude=None, docs=None, site_files=None, site_repo=None) -> dict:
    """Fill the FactStore's code -> docs coupling for a repo that is already loaded, WITHOUT embedding anything: public symbols of
    every in-scope file, and which documents mention them. Safe to re-run. `site_files` is {path: text}."""
    scope, exclude, docs = scope or ["**"], exclude or [], docs or []
    all_files = git.list_files(commit)
    allowed = [f for f in select_files(all_files, scope, exclude) if not DOC_FILE.match(f)]
    rows: list[dict] = []
    for f in allowed:
        raw = git.read_at(commit, f)
        if _binary_or_big(raw):
            continue
        for sy in extract_public_symbols(f, raw, calls=True):
            rows.append({"path": f, "kind": sy["kind"], "name": sy["name"], "sig_hash": sig_hash(sy["sig"])})
    facts.replace_symbols(repo, rows)
    known = facts.known_symbol_names()
    refs = 0
    doc_count = 0
    for f in [x for x in all_files if (DOC_FILE.match(x) or matches_any(x, docs)) and not matches_any(x, exclude)]:
        raw = git.read_at(commit, f)
        if raw is None:
            continue
        refs += record_doc_refs(facts, repo, f, raw, "source_doc", known)
        doc_count += 1
    site_refs = 0
    if site_files:
        for path, text in site_files.items():
            site_refs += record_doc_refs(facts, f"site:{site_repo}", path, text, "site_doc", known)
    return {"symbols": len(rows), "kinds": list(dict.fromkeys(r["kind"] for r in rows)), "files": len(allowed), "docFiles": doc_count,
            "docRefs": refs, "siteRefs": site_refs}


def retrieve_code(*, llm, store, repo, queries, top_k, exclude=None, budget_chars=30000, min_score=0) -> list[dict]:
    """Code chunks relevant to the given queries from across the whole repo, skipping files the caller already has in full,
    within a character budget."""
    exclude = exclude or []
    vecs = llm.embed([q for q in queries if q])
    seen: dict = {}
    for v in vecs:
        for h in store.search(v, limit=top_k * 2, repo=repo, kind="code"):
            if h["payload"]["path"] in exclude or h["score"] < min_score:
                continue  # marginal matches are noise, not context
            prev = seen.get(h["id"])
            if not prev or h["score"] > prev["score"]:
                p = h["payload"]
                seen[h["id"]] = {"id": h["id"], "score": h["score"], "path": p["path"], "start": p.get("start"), "end": p.get("end"), "text": p["text"]}
    ranked = sorted(seen.values(), key=lambda c: -c["score"])[:top_k]
    out, used = [], 0
    for c in ranked:
        if used + len(c["text"]) > budget_chars:
            break
        used += len(c["text"])
        out.append(c)
    return out


def retrieve_semantic(*, llm, store, repo, queries, top_k, site_repo=None, site_top_k=3, min_score=0) -> list[dict]:
    """Semantic context: approved pages, source docs and briefs for this repo."""
    vecs = llm.embed([q for q in queries if q])
    seen: dict = {}
    for v in vecs:
        hits = store.search(v, limit=top_k * 2, repo=repo, kind=["approved", "source_doc", "brief"])
        # the rest of the documentation site: terminology, structure, what is already covered elsewhere
        if site_repo:
            hits = [*hits, *store.search(v, limit=site_top_k, repo=f"site:{site_repo}", kind="site_doc")]
        for h in hits:
            if h["payload"].get("kind") != "brief" and h["score"] < min_score:
                continue  # the page brief is always kept; everything else must be relevant
            prev = seen.get(h["id"])
            if not prev or h["score"] > prev["score"]:
                p = h["payload"]
                seen[h["id"]] = {"id": h["id"], "score": h["score"], "kind": p.get("kind"), "path": p.get("path"), "heading": p.get("heading"), "text": p["text"]}
    return sorted(seen.values(), key=lambda c: -c["score"])[: top_k + (site_top_k if site_repo else 0)]
