"""Integration tests against REAL Qdrant and Postgres. Skipped unless both are configured:
  QDRANT_URL [QDRANT_API_KEY]  FACTSTORE_DATABASE_URL
Safe to point at a shared instance: every test uses unique names (random collections, `itest/<id>` repos) and removes what it created.
The LLM is a local fake, so no API key is needed.

  QDRANT_URL=http://127.0.0.1:6333 FACTSTORE_DATABASE_URL=postgres://... pytest tests/integration
"""
import json
import os
import secrets
import subprocess
import sys

import httpx
import pytest

from multisync.factstore import PgFactStore
from multisync.testing import FakeOpenAI
from multisync.vectorstore import QdrantStore

LIVE = bool(os.environ.get("QDRANT_URL") and os.environ.get("FACTSTORE_DATABASE_URL"))
pytestmark = pytest.mark.skipif(not LIVE, reason="set QDRANT_URL and FACTSTORE_DATABASE_URL to run")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def uid():
    return secrets.token_hex(4)


def qcfg(collection):
    return {"url": os.environ["QDRANT_URL"].rstrip("/"), "apiKey": os.environ.get("QDRANT_API_KEY", ""), "collection": collection}


def drop_collection(name):
    try:
        httpx.delete(f"{qcfg(name)['url']}/collections/{name}", headers={"api-key": os.environ["QDRANT_API_KEY"]} if os.environ.get("QDRANT_API_KEY") else {})
    except httpx.HTTPError:
        pass


def vec(i, dim=8):
    return [1 if k == i % dim else 0.01 for k in range(dim)]


def test_qdrant_real_collection_upsert_filtered_search_rekey_delete_count():
    name = f"itest_{uid()}"
    q = QdrantStore(qcfg(name), 8)
    try:
        assert q.health()
        q.ensure_collection()
        q.ensure_collection()  # idempotent
        pid = lambda n: f"00000000-0000-4000-8000-{n:012d}"  # noqa: E731
        q.upsert([
            {"id": pid(1), "vector": vec(0), "payload": {"repo": "o/a", "path": "x.md", "kind": "approved", "commit": "c1", "text": "one"}},
            {"id": pid(2), "vector": vec(1), "payload": {"repo": "o/a", "path": "y.go", "kind": "code", "commit": "c1", "text": "two"}},
            {"id": pid(3), "vector": vec(0), "payload": {"repo": "o/b", "path": "x.md", "kind": "approved", "commit": "c1", "text": "three"}},
        ])
        assert q.count() == 3
        hits = q.search(vec(0), limit=5, repo="o/a")
        assert all(h["payload"]["repo"] == "o/a" for h in hits), "repo filter"
        assert hits[0]["payload"]["text"] == "one"
        assert [h["payload"]["path"] for h in q.search(vec(1), repo="o/a", kind="code")] == ["y.go"]
        assert [h["payload"]["path"] for h in q.search(vec(0), repo="o/a", kind=["approved", "brief"])] == ["x.md"]
        assert len(q.search(vec(0), repo="o/a", path="nope.md")) == 0
        q.touch_commit([pid(1)], "c2")
        assert q.search(vec(0), repo="o/a", kind="approved")[0]["payload"]["commit"] == "c2"
        q.delete_by_path("o/a", "x.md")
        assert q.count("o/a") == 1
        q.delete_by_repo("o/a", "code")
        assert q.count("o/a") == 0
        assert q.count("o/b") == 1, "other repos untouched"
    finally:
        drop_collection(name)


def test_factstore_real_postgres_schema_is_isolated_migrate_is_idempotent_everything_round_trips():
    f = PgFactStore(os.environ["FACTSTORE_DATABASE_URL"])
    repo = f"itest/{uid()}"
    try:
        f.migrate()
        f.migrate()
        assert f.health()
        schemas = [r["table_schema"] for r in f._q("SELECT table_schema FROM information_schema.tables WHERE table_name='claims'")]
        assert "multisync" in schemas, "tables live in the multisync schema"
        assert f._q("SELECT current_schema() AS s")[0]["s"] == "multisync", "every connection is pinned to the schema"

        f.save_claims(repo, "a.md", "c1", [{"text": "port is 8080 for alert_channels_enum", "supported": True}, {"text": "bad", "supported": False}])
        assert f.approved_claims(repo, "a.md") == [], "nothing is trusted before approval"
        assert f.repo_documents_symbol(repo, "alert_channels_enum") is False
        assert f._q("SELECT count(*)::int AS n FROM multisync.claims WHERE repo=%s", (repo,))[0]["n"] == 2
        f.approve_claims(repo, "a.md", "c1")
        assert f.approved_claims(repo, "a.md") == ["port is 8080 for alert_channels_enum"], "only supported claims are approved"
        assert f.repo_documents_symbol(repo, "ALERT_CHANNELS_enum") is True, "case-insensitive cross-repo lookup"

        f.record_decision({"runId": "r1", "repo": repo, "path": "a.md", "commit": "c1", "outcome": "fallback", "reviewerAction": "auto_rejected",
                           "rootCauseTag": "iteration_cap_exceeded", "reason": "x", "metrics": {"a": 1}, "attempts": [{"n": 1}]})
        assert f.root_cause_backlog(repo) == [{"root_cause_tag": "iteration_cap_exceeded", "n": 1}]

        f.record_node_log({"runId": "r1", "repo": repo, "path": "a.md", "commit": "c1", "node": "judge", "status": "ok", "ms": 12, "note": {"precision": 1}})
        log = f._q("SELECT node, note FROM node_logs WHERE run_id=%s AND repo=%s", ("r1", repo))[0]
        assert log["node"] == "judge"
        assert log["note"]["precision"] == 1, "jsonb note round-trips"

        assert f.get_context_state(repo) is None
        f.set_context_state(repo, "c1", 3, 9)
        f.set_context_state(repo, "c2", 4, 12)
        assert f.get_context_state(repo) == {"repo": repo, "commit": "c2", "files": 4, "chunks": 12}

        # code -> docs coupling
        f.replace_symbols(repo, [{"path": "a.ts", "kind": "export", "name": "createPoll", "sig_hash": "h1"}, {"path": "a.ts", "kind": "export", "name": "createPoll", "sig_hash": "h1"}])
        f.replace_doc_refs(f"{repo}-docs", "docs/g.md", ["createPoll"], "source_doc")
        assert "createPoll" in f.known_symbol_names()
        assert [r["doc_repo"] for r in f.doc_refs(["createPoll"], exclude_repo=repo)] == [f"{repo}-docs"]
        info = f.symbol_info("createPoll")
        assert {"repo": repo, "path": "a.ts", "kind": "export"} in info["defined"]
        assert any(r["doc_repo"] == f"{repo}-docs" for r in info["referencedBy"])

        # facts about the repository as a whole, and the central run view
        f.replace_repo_facts(repo, "deterministic", [{"category": "language", "fact": "x is mainly written in Go.", "evidence": "go.mod"}], "c1", None)
        f.replace_repo_facts(repo, "llm", [{"category": "purpose", "fact": "x is a dashboard.", "evidence": "README"}], "c1", "h1")
        assert {r["source"] for r in f.repo_facts(repo)} == {"deterministic", "llm"}
        f.replace_repo_facts(repo, "deterministic", [], "c2", None)
        assert [r["source"] for r in f.repo_facts(repo)] == ["llm"], "replacing one source leaves the other"
        runs = f.list_runs(5, repo)
        assert runs and runs[0]["run_id"] == "r1" and runs[0]["nodes"] == 1 and runs[0]["outcomes"] == {"fallback": 1}
        assert [x["node"] for x in f.run_logs("r1") if x["repo"] == repo] == ["judge"]
    finally:
        for t in ("claims", "decisions", "node_logs", "context_state", "symbols", "repo_facts"):
            f._q(f"DELETE FROM {t} WHERE repo=%s", (repo,))
        f._q("DELETE FROM doc_refs WHERE doc_repo LIKE %s", (f"{repo}%",))
        f.close()


def git(src, *a):
    return subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True, text=True).stdout


def test_end_to_end_real_qdrant_and_postgres_bootstrap_then_a_code_mode_run_publishes_and_logs_every_stage(tmp_path):
    fake = FakeOpenAI()
    src = tmp_path / "source-repo"
    repo = f"itest/{uid()}"
    docs_c, code_c = f"itest_docs_{uid()}", f"itest_code_{uid()}"
    f = PgFactStore(os.environ["FACTSTORE_DATABASE_URL"])
    run_id = f"itest-{uid()}"
    try:
        (src / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        git(src, "config", "user.email", "t@t")
        git(src, "config", "user.name", "t")
        (src / "README.md").write_text("# Alerts\n\nAn alert service.\n")
        (src / "src/a.go").write_text("package main\nfunc Alert() {}\n")
        (src / "src/b.go").write_text("package main\nfunc Other() { Alert() }\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "one")
        c1 = git(src, "rev-parse", "HEAD").strip()
        (src / "src/a.go").write_text('package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n')
        git(src, "add", "-A")
        git(src, "commit", "-qm", "two")
        c2 = git(src, "rev-parse", "HEAD").strip()

        (tmp_path / "repos.json").write_text(json.dumps({"repos": {repo: {"mode": "code", "trust": "auto", "serviceName": "proj", "pages": [{"path": "api.md", "kind": "API documentation", "brief": "Explain the alert API."}]}}}))
        env = {**os.environ, "PYTHONPATH": ROOT, "AI_API_BASE_URL": fake.url, "INTERNAL_AI_API_KEY": "x", "AI_EMBED_DIM": "64", "QDRANT_COLLECTION": docs_c, "QDRANT_CODE_COLLECTION": code_c,
               "REPOS_CONFIG": str(tmp_path / "repos.json"), "FEATURE_REGISTRY": str(tmp_path / "none.json"), "DOCS_ROOT": "site/docs",
               "TEMPLATES_PATH": os.path.join(ROOT, "docs/templates"), "INSTRUCTIONS_FILE": os.path.join(ROOT, ".github/instructions/DocumentationInstructions.instructions.md"),
               "SOURCE_REPO": repo, "SOURCE_DIR": str(src), "CHANGED_FILES": "", "TICKET_PROVIDER": "none", "RUN_ID": run_id}
        env.pop("VECTOR_DRIVER", None)
        env.pop("FACTSTORE_DRIVER", None)

        # 1. bootstrap: the WHOLE context is in the vector DB before any sync
        boot = subprocess.run([sys.executable, "-m", "multisync.cli.bootstrap_context", "--repo", repo, "--dir", str(src), "--ref", c1], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert boot.returncode == 0, boot.stdout + boot.stderr
        assert "bootstrapped" in boot.stdout and ": 2 files," in boot.stdout
        code, docs = QdrantStore(qcfg(code_c), 64, collection=code_c), QdrantStore(qcfg(docs_c), 64, collection=docs_c)
        assert code.count(repo) >= 2, "both source files are in the code collection"
        assert docs.count(repo) >= 2, "README and the page brief are in the semantic collection"
        assert f.get_context_state(repo)["commit"] == c1

        # 2. a real sync of commit c2
        r = subprocess.run([sys.executable, "-m", "multisync.cli.run_pipeline"], cwd=tmp_path, env={**env, "SOURCE_SHA": c2, "SOURCE_BEFORE": c1}, capture_output=True, text=True)
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert '[sync_context] ok' in out and '"mode": "incremental"' in out
        res = json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]
        assert res["outcome"] == "published"
        assert (tmp_path / res["targetPath"]).exists()
        assert f.get_context_state(repo)["commit"] == c2, "index advanced to the new commit"
        assert fake.hits["analyze"] == 1 and fake.hits["plan"] == 1 and fake.hits["judge"] >= 1

        # 3. audit trail is in Postgres, in order, and the approved page is now in the semantic index
        nodes = [x["node"] for x in f._q("SELECT node FROM node_logs WHERE run_id=%s AND repo=%s ORDER BY id", (run_id, repo))]
        assert nodes == ["sync_context", "repo_facts", "prefilter", "cross_repo", "route", "similarity", "code_context", "gar", "semantic_context", "write_draft", "verify_draft", "judge", "publish"]
        assert f._q("SELECT outcome FROM decisions WHERE run_id=%s AND repo=%s", (run_id, repo))[0]["outcome"] == "published"
        assert len(docs.search([0.1] * 64, limit=20, repo=repo, kind="approved")) >= 1, "auto-trust publish indexed the approved page"
        assert len(f.approved_claims(repo, "api.md")) >= 1, "claims were approved into the FactStore"
    finally:
        fake.close()
        for c in (docs_c, code_c):
            drop_collection(c)
        for t in ("claims", "decisions", "node_logs", "context_state", "symbols"):
            try:
                f._q(f"DELETE FROM {t} WHERE repo=%s", (repo,))
            except Exception:  # noqa: BLE001
                pass
        f._q("DELETE FROM doc_refs WHERE doc_repo=%s", (repo,))
        f.close()
