"""FactStore: Postgres-backed store of atomic claims plus the decision audit log and the code->docs coupling tables.
(This is our own component, not the academic FactScore method.)

Tables are defined in infra/postgres/init.sql and created by migrate().
"""
from __future__ import annotations

import json
from pathlib import Path

INIT_SQL = Path(__file__).resolve().parent.parent / "infra" / "postgres" / "init.sql"


class PgFactStore:
    def __init__(self, database_url: str):
        import psycopg  # lazy: unit tests and memory mode do not need a database driver
        from psycopg.rows import dict_row

        # Pin the schema on every connection so queries never touch other workloads' tables.
        self.conn = psycopg.connect(database_url, options="-c search_path=multisync", autocommit=True, row_factory=dict_row)

    def _q(self, sql: str, params=None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def migrate(self) -> None:
        self.conn.execute(INIT_SQL.read_text(encoding="utf-8"))

    def health(self) -> bool:
        self._q("SELECT 1")
        return True

    def save_claims(self, repo, file_path, commit, claims) -> None:
        for c in claims:
            self._q("INSERT INTO claims (repo, path, commit, claim, supported, evidence) VALUES (%s,%s,%s,%s,%s,%s)",
                    (repo, file_path, commit, c["text"], bool(c.get("supported")), c.get("evidence") or None))

    def approved_claims(self, repo, file_path, limit=50) -> list[str]:
        """Previously approved claims for a document (used as extra judge evidence)."""
        rows = self._q("SELECT claim FROM claims WHERE repo=%s AND path=%s AND supported AND approved ORDER BY created_at DESC LIMIT %s",
                       (repo, file_path, limit))
        return [r["claim"] for r in rows]

    def approve_claims(self, repo, file_path, commit) -> None:
        self._q("UPDATE claims SET approved=true WHERE repo=%s AND path=%s AND commit=%s AND supported", (repo, file_path, commit))

    def repo_documents_symbol(self, repo, symbol) -> bool:
        """True when an approved document of `repo` mentions `symbol`. Used by the cross-repo gate."""
        return bool(self._q("SELECT 1 FROM claims WHERE repo=%s AND approved AND claim ILIKE %s LIMIT 1", (repo, f"%{symbol}%")))

    def record_decision(self, d) -> None:
        self._q(
            "INSERT INTO decisions (run_id, repo, path, commit, outcome, reviewer_action, root_cause_tag, reason, metrics, attempts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (d.get("runId"), d.get("repo"), d.get("path"), d.get("commit"), d.get("outcome"), d.get("reviewerAction"),
             d.get("rootCauseTag") or None, d.get("reason"), json.dumps(d.get("metrics") or {}), json.dumps(d.get("attempts") or [])))

    def get_context_state(self, repo):
        rows = self._q("SELECT repo, commit, files, chunks FROM context_state WHERE repo=%s", (repo,))
        return rows[0] if rows else None

    def list_context_state(self):
        return self._q("SELECT repo, commit, files, chunks, updated_at FROM context_state ORDER BY repo")

    def set_context_state(self, repo, commit, files, chunks) -> None:
        self._q("INSERT INTO context_state (repo, commit, files, chunks) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (repo) DO UPDATE SET commit=%s, files=%s, chunks=%s, updated_at=now()",
                (repo, commit, files, chunks, commit, files, chunks))

    def record_node_log(self, e) -> None:
        self._q("INSERT INTO node_logs (run_id, repo, path, commit, node, status, ms, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (e.get("runId"), e.get("repo"), e.get("path"), e.get("commit"), e.get("node"), e.get("status"), e.get("ms"), json.dumps(e.get("note") or {})))

    def root_cause_backlog(self, repo):
        """Repeated fallbacks per repo/tag: feeds the root-cause backlog."""
        return self._q("SELECT root_cause_tag, count(*)::int AS n FROM decisions WHERE repo=%s AND outcome='fallback' GROUP BY 1 ORDER BY n DESC", (repo,))

    # ── code -> docs coupling ────────────────────────────────────────────────────
    def replace_symbols(self, repo, rows, paths=None) -> None:
        """Replace a repo's symbols: only `paths` when given (incremental), otherwise all of them (full re-index)."""
        if paths:
            self._q("DELETE FROM symbols WHERE repo=%s AND path = ANY(%s)", (repo, list(paths)))
        else:
            self._q("DELETE FROM symbols WHERE repo=%s", (repo,))
        for i in range(0, len(rows), 500):
            b = rows[i:i + 500]
            self._q("INSERT INTO symbols (repo, path, kind, name, sig_hash) "
                    "SELECT %s, * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) ON CONFLICT DO NOTHING",
                    (repo, [r["path"] for r in b], [r["kind"] for r in b], [r["name"] for r in b], [r.get("sig_hash") or "" for r in b]))

    def known_symbol_names(self) -> set[str]:
        return {r["name"] for r in self._q("SELECT DISTINCT name FROM symbols")}

    def replace_doc_refs(self, doc_repo, doc_path, symbols, kind) -> None:
        self._q("DELETE FROM doc_refs WHERE doc_repo=%s AND doc_path=%s", (doc_repo, doc_path))
        if not symbols:
            return
        self._q("INSERT INTO doc_refs (symbol, doc_repo, doc_path, kind) SELECT unnest(%s::text[]), %s, %s, %s ON CONFLICT DO NOTHING",
                (list(symbols), doc_repo, doc_path, kind))

    def doc_refs(self, names, exclude_repo=None):
        """Documents that mention any of `names`, optionally leaving out one repo's own documents."""
        if not names:
            return []
        return self._q("SELECT symbol, doc_repo, doc_path, kind FROM doc_refs WHERE symbol = ANY(%s) AND (%s::text IS NULL OR doc_repo <> %s) "
                       "ORDER BY symbol, doc_repo, doc_path", (list(names), exclude_repo, exclude_repo))

    def symbol_info(self, name):
        defs = self._q("SELECT repo, path, kind FROM symbols WHERE name=%s ORDER BY repo, path", (name,))
        refs = self._q("SELECT doc_repo, doc_path, kind FROM doc_refs WHERE symbol=%s ORDER BY doc_repo, doc_path", (name,))
        return {"defined": defs, "referencedBy": refs}

    def coupling_stats(self):
        return self._q("SELECT s.repo, count(*)::int AS symbols, count(DISTINCT s.kind)::int AS kinds FROM symbols s GROUP BY s.repo ORDER BY s.repo")

    # ── central view of runs ─────────────────────────────────────────────────────
    def list_runs(self, limit=20, repo=None):
        """Most recent runs: {run_id, repo, started, ended, nodes, errors, usd, outcomes}."""
        where, args = ("WHERE repo=%s", [repo]) if repo else ("", [])
        rows = self._q(
            "SELECT run_id, repo, min(created_at) AS started, max(created_at) AS ended, count(*)::int AS nodes, "
            "count(*) FILTER (WHERE status='error')::int AS errors, "
            "coalesce(sum(coalesce((note#>>'{usage,cheap,usd}')::float,0) + coalesce((note#>>'{usage,expensive,usd}')::float,0)),0) AS usd "
            f"FROM node_logs {where} GROUP BY run_id, repo ORDER BY min(created_at) DESC LIMIT %s", args + [limit])
        for r in rows:
            r["outcomes"] = {o["outcome"]: o["n"] for o in self._q("SELECT outcome, count(*)::int AS n FROM decisions WHERE run_id=%s GROUP BY outcome", (r["run_id"],))}
        return rows

    def run_logs(self, run_id):
        return self._q("SELECT repo, path, commit, node, status, ms, note, created_at FROM node_logs WHERE run_id=%s ORDER BY id", (run_id,))

    # ── facts about a repository as a whole ──────────────────────────────────────
    def replace_repo_facts(self, repo, source, rows, commit) -> None:
        """Replace all facts of one source (deterministic | llm) for a repo."""
        self._q("DELETE FROM repo_facts WHERE repo=%s AND source=%s", (repo, source))
        self.add_repo_facts(repo, rows, commit)

    def delete_repo_facts(self, repo, source, source_paths=None) -> None:
        if source_paths is None:
            self._q("DELETE FROM repo_facts WHERE repo=%s AND source=%s", (repo, source))
        else:
            self._q("DELETE FROM repo_facts WHERE repo=%s AND source=%s AND source_path = ANY(%s)", (repo, source, list(source_paths)))

    def add_repo_facts(self, repo, rows, commit) -> None:
        for r in rows:
            self._q("INSERT INTO repo_facts (repo, category, fact, evidence, source, source_path, source_hash, extracted_at, verification_method, flag, flag_detail, commit) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (repo, fact) DO NOTHING",
                    (repo, r["category"], r["fact"], r["evidence"], r["source"], r.get("source_path", ""), r.get("source_hash", ""), r.get("extracted_at") or "now",
                     r.get("verification_method", "deterministic"), r.get("flag"), r.get("flag_detail"), commit))

    def set_repo_fact_flags(self, repo, rows) -> None:
        for r in rows:
            self._q("UPDATE repo_facts SET flag=%s, flag_detail=%s WHERE repo=%s AND fact=%s", (r.get("flag"), r.get("flag_detail"), repo, r["fact"]))

    def repo_facts(self, repo, source=None):
        sql, args = ("SELECT category, fact, evidence, source, source_path, source_hash, extracted_at, verification_method, flag, flag_detail, commit "
                     "FROM repo_facts WHERE repo=%s"), [repo]
        if source:
            sql, args = sql + " AND source=%s", args + [source]
        return self._q(sql + " ORDER BY source, category, id", args)

    def close(self) -> None:
        self.conn.close()


class MemoryFactStore:
    def __init__(self):
        self.claims: list[dict] = []
        self.decisions: list[dict] = []
        self.node_logs: list[dict] = []
        self.ctx: dict = {}
        self.symbols: list[dict] = []
        self.refs: list[dict] = []

    def list_runs(self, limit=20, repo=None):
        runs: dict = {}
        for e in self.node_logs:
            if repo and e.get("repo") != repo:
                continue
            r = runs.setdefault(e["runId"], {"run_id": e["runId"], "repo": e.get("repo"), "started": e.get("at"), "ended": e.get("at"), "nodes": 0, "errors": 0, "usd": 0.0, "outcomes": {}})
            r["nodes"] += 1
            r["errors"] += e.get("status") == "error"
            u = (e.get("note") or {}).get("usage") or {}
            r["usd"] += sum((u.get(t) or {}).get("usd", 0) for t in ("cheap", "expensive"))
            r["ended"] = e.get("at")
        for d in self.decisions:
            if d.get("runId") in runs:
                o = runs[d["runId"]]["outcomes"]
                o[d["outcome"]] = o.get(d["outcome"], 0) + 1
        return list(runs.values())[-limit:][::-1]

    def run_logs(self, run_id):
        return [{"repo": e.get("repo"), "path": e.get("path"), "commit": e.get("commit"), "node": e["node"], "status": e["status"], "ms": e["ms"], "note": e.get("note") or {},
                 "created_at": e.get("at")} for e in self.node_logs if e.get("runId") == run_id]

    def replace_repo_facts(self, repo, source, rows, commit):
        self.rfacts = [r for r in getattr(self, "rfacts", []) if not (r["repo"] == repo and r["source"] == source)]
        self.add_repo_facts(repo, rows, commit)

    def delete_repo_facts(self, repo, source, source_paths=None):
        self.rfacts = [r for r in getattr(self, "rfacts", []) if not (r["repo"] == repo and r["source"] == source and (source_paths is None or r.get("source_path") in source_paths))]

    def add_repo_facts(self, repo, rows, commit):
        self.rfacts = getattr(self, "rfacts", [])
        for r in rows:
            if not any(x["repo"] == repo and x["fact"] == r["fact"] for x in self.rfacts):
                self.rfacts.append({"repo": repo, "flag": None, "flag_detail": None, "source_path": "", "source_hash": "", "extracted_at": None, "verification_method": "deterministic", **r, "commit": commit})

    def set_repo_fact_flags(self, repo, rows):
        for r in rows:
            for x in getattr(self, "rfacts", []):
                if x["repo"] == repo and x["fact"] == r["fact"]:
                    x["flag"], x["flag_detail"] = r.get("flag"), r.get("flag_detail")

    def repo_facts(self, repo, source=None):
        return [dict(r) for r in getattr(self, "rfacts", []) if r["repo"] == repo and (not source or r["source"] == source)]

    def get_context_state(self, repo): return self.ctx.get(repo)
    def set_context_state(self, repo, commit, files, chunks): self.ctx[repo] = {"repo": repo, "commit": commit, "files": files, "chunks": chunks}
    def list_context_state(self): return list(self.ctx.values())
    def record_node_log(self, e): self.node_logs.append(e)
    def migrate(self): pass
    def health(self): return True
    def close(self): pass

    def save_claims(self, repo, file_path, commit, claims):
        for c in claims:
            self.claims.append({"repo": repo, "path": file_path, "commit": commit, "claim": c["text"], "supported": bool(c.get("supported")), "approved": False})

    def approved_claims(self, repo, file_path, limit=50):
        return [c["claim"] for c in self.claims if c["repo"] == repo and c["path"] == file_path and c["supported"] and c["approved"]][:limit]

    def approve_claims(self, repo, file_path, commit):
        for c in self.claims:
            if c["repo"] == repo and c["path"] == file_path and c["commit"] == commit and c["supported"]:
                c["approved"] = True

    def repo_documents_symbol(self, repo, symbol):
        return any(c["repo"] == repo and c["approved"] and symbol.lower() in c["claim"].lower() for c in self.claims)

    def record_decision(self, d): self.decisions.append(d)

    def root_cause_backlog(self, repo):
        m: dict = {}
        for d in self.decisions:
            if d.get("repo") == repo and d.get("outcome") == "fallback":
                m[d.get("rootCauseTag")] = m.get(d.get("rootCauseTag"), 0) + 1
        return [{"root_cause_tag": k, "n": v} for k, v in m.items()]

    # ── code -> docs coupling ────────────────────────────────────────────────────
    def replace_symbols(self, repo, rows, paths=None):
        self.symbols = [r for r in self.symbols if not (r["repo"] == repo and (not paths or r["path"] in paths))]
        for r in rows:
            if not any(x["repo"] == repo and x["path"] == r["path"] and x["kind"] == r["kind"] and x["name"] == r["name"] for x in self.symbols):
                self.symbols.append({"repo": repo, **r})

    def known_symbol_names(self): return {r["name"] for r in self.symbols}

    def replace_doc_refs(self, doc_repo, doc_path, symbols, kind):
        self.refs = [r for r in self.refs if not (r["doc_repo"] == doc_repo and r["doc_path"] == doc_path)]
        for s in symbols:
            self.refs.append({"symbol": s, "doc_repo": doc_repo, "doc_path": doc_path, "kind": kind})

    def doc_refs(self, names, exclude_repo=None):
        return [r for r in self.refs if r["symbol"] in names and (not exclude_repo or r["doc_repo"] != exclude_repo)]

    def symbol_info(self, name):
        return {
            "defined": [{"repo": r["repo"], "path": r["path"], "kind": r["kind"]} for r in self.symbols if r["name"] == name],
            "referencedBy": [{"doc_repo": r["doc_repo"], "doc_path": r["doc_path"], "kind": r["kind"]} for r in self.refs if r["symbol"] == name],
        }

    def coupling_stats(self):
        m: dict = {}
        for r in self.symbols:
            e = m.setdefault(r["repo"], {"repo": r["repo"], "symbols": 0, "kinds": set()})
            e["symbols"] += 1
            e["kinds"].add(r["kind"])
        return [{"repo": e["repo"], "symbols": e["symbols"], "kinds": len(e["kinds"])} for e in m.values()]


def create_fact_store(cfg):
    return PgFactStore(cfg["databaseUrl"]) if cfg["driver"] == "postgres" else MemoryFactStore()
