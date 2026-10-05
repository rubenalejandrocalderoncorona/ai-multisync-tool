import re
from datetime import datetime, timezone

import pytest

from multisync import writer as W
from multisync.chunker import chunk_code
from multisync.cli.context_search import context_status, search_context
from multisync.codesource import build_code_changes, pages_scope
from multisync.context import retrieve_code, retrieve_semantic, sync_context, sync_site
from multisync.coverage import check_coverage
from multisync.critic import evaluate, judge
from multisync.factstore import MemoryFactStore
from multisync.pipeline import process_change
from multisync.prompts import load_prompt, load_styles
from multisync.sitedocs import site_files
from multisync.stages import analyze_code, plan_docs
from multisync.testing import CODE_FACTS, HALLUCINATION_JUDGE, PASS_JUDGE, fake_llm, make_deps
from multisync.vectorstore import MemoryVectorStore


# ── a tiny in-memory "git" ───────────────────────────────────────────────────
class Repo:
    def __init__(self, tree):
        self.tree = tree

    def list_files(self, rev):
        return list(self.tree.get(rev, {}))

    def read_at(self, rev, f):
        return self.tree.get(rev, {}).get(f)

    def changed_between(self, a, b):
        files = list(dict.fromkeys([*self.tree.get(a, {}), *self.tree.get(b, {})]))
        return [f for f in files if self.tree.get(a, {}).get(f) != self.tree.get(b, {}).get(f)]


def setup():
    return dict(code_store=MemoryVectorStore(), doc_store=MemoryVectorStore(), llm=fake_llm([PASS_JUDGE]), facts=MemoryFactStore())


T = {
    "c1": {"src/a.go": "package main\nfunc Alert() {}\n", "src/b.go": "package main\nfunc Other() {}\n", "README.md": "# Proj\n\nAn alert service.\n",
           "src/a_test.go": "package main\nfunc TestA() {}\n", "package-lock.json": "{}"},
    "c2": {"src/a.go": "package main\nfunc Alert() {}\nfunc Silence() {}\n", "src/b.go": "package main\nfunc Other() {}\n", "README.md": "# Proj\n\nAn alert service.\n",
           "src/a_test.go": "package main\nfunc TestA() {}\n", "package-lock.json": "{}"},
    "c3": {"src/a.go": "package main\nfunc Alert() {}\nfunc Silence() {}\n", "README.md": "# Proj\n\nAn alert service.\n"},
}


def test_chunk_code_windows_overlap_carry_their_location_and_cut_on_blank_lines():
    body = "\n".join("" if i % 40 == 39 else f"line {i + 1}" for i in range(200))
    chunks = chunk_code("src/x.go", body, max_lines=50, overlap=5)
    assert len(chunks) >= 4
    assert re.match(r"^FILE src/x\.go lines 1-", chunks[0]["text"])
    assert chunks[1]["start"] <= chunks[0]["end"], "chunks overlap"
    assert chunks[-1]["end"] == 200
    assert chunk_code("e.go", "\n\n") == []


def test_sync_context_first_run_loads_the_whole_repo_into_both_collections_skipping_tests_and_lockfiles():
    s = setup()
    r = sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s, pages=[{"path": "overview.md", "brief": "Explain alerting."}])
    assert r["mode"] == "full"
    assert "no previous commit" in r["reason"]
    assert r["repoFiles"] == 2  # a.go, b.go (README is semantic; tests and lockfile excluded)
    assert sorted({p["payload"]["path"] for p in s["code_store"].points.values()}) == ["src/a.go", "src/b.go"]
    assert all(p["payload"]["kind"] == "code" and p["payload"]["commit"] == "c1" for p in s["code_store"].points.values())
    assert sorted(p["payload"]["kind"] for p in s["doc_store"].points.values()) == ["brief", "source_doc"]
    assert s["facts"].get_context_state("o/r")["commit"] == "c1"


def test_sync_context_when_the_index_is_at_the_previous_commit_only_changed_files_are_reembedded():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s)
    before = s["llm"].calls["embed"]
    r = sync_context(repo="o/r", commit="c2", before="c1", git=Repo(T), **s)
    assert r["mode"] == "incremental"
    assert r["filesIndexed"] == 1, "only src/a.go changed"
    assert s["llm"].calls["embed"] - before >= 1
    a = [p for p in s["code_store"].points.values() if p["payload"]["path"] == "src/a.go"]
    assert all(p["payload"]["commit"] == "c2" for p in a) and any("Silence" in p["payload"]["text"] for p in a)
    assert all(p["payload"]["commit"] == "c1" for p in s["code_store"].points.values() if p["payload"]["path"] == "src/b.go"), "untouched files keep their chunks"


def test_sync_context_removed_files_leave_the_index():
    s = setup()
    sync_context(repo="o/r", commit="c2", before="", git=Repo(T), **s)
    r = sync_context(repo="o/r", commit="c3", before="c2", git=Repo(T), **s)
    assert r["removed"] == 1
    assert not any(p["payload"]["path"] == "src/b.go" for p in s["code_store"].points.values())


def test_sync_context_self_heals_with_a_full_reindex_when_the_index_is_not_at_the_previous_commit():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s)
    # a run was missed: the index says c1 but this run claims the previous commit was c2
    r = sync_context(repo="o/r", commit="c3", before="c2", git=Repo(T), **s)
    assert r["mode"] == "full"
    assert "index at c1" in r["reason"]
    assert not any(p["payload"]["path"] == "src/b.go" for p in s["code_store"].points.values()), "stale chunks are gone"


def test_sync_context_other_repos_are_never_touched():
    s = setup()
    sync_context(repo="o/other", commit="c1", before="", git=Repo(T), **s)
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), full=True, **s)
    assert s["code_store"].count("o/other") > 0


def test_sync_context_secret_looking_lines_never_reach_the_embedding_input_or_the_index():
    s = setup()
    seen = []

    def embed(texts):
        seen.extend(texts)
        return [[1, 0] for _ in texts]

    s["llm"].embed = embed
    sync_context(repo="o/r", commit="c1", before="", git=Repo({"c1": {"a.go": 'x := 1\nconst API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n'}}), **s)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in "\n".join(seen)
    assert not any("abcdefghijklmnopqrstuvwxyz123456" in p["payload"]["text"] for p in s["code_store"].points.values())


def test_retrieve_code_searches_the_whole_repo_index_excludes_supplied_files_respects_the_budget():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s)
    kw = dict(llm=s["llm"], store=s["code_store"], repo="o/r")
    assert len(retrieve_code(queries=["func Alert"], top_k=10, **kw)) >= 1
    assert all(c["path"] != "src/a.go" for c in retrieve_code(queries=["func Alert"], top_k=10, exclude=["src/a.go"], **kw))
    assert len(retrieve_code(queries=["x"], top_k=10, budget_chars=5, **kw)) == 0


def test_retrieve_semantic_returns_approved_pages_source_docs_and_briefs_never_code():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s, pages=[{"path": "overview.md", "brief": "Explain alerting"}])
    hits = retrieve_semantic(llm=s["llm"], store=s["doc_store"], repo="o/r", queries=["alert service"], top_k=5)
    assert len(hits) >= 1 and all(h["kind"] in ("approved", "source_doc", "brief") for h in hits)


# ── stage contracts ──────────────────────────────────────────────────────────
def test_analyze_code_facts_without_evidence_are_dropped_ids_are_kept():
    llm = fake_llm()
    r = analyze_code(llm, page="overview.md", style_key="API documentation", changed_files=["src/a.go"], repo_map=["src/a.go", "src/b.go"],
                     code="### FILE: src/a.go\nx", related=[{"text": "FILE src/b.go lines 1-2\ny"}])
    assert [f["id"] for f in r["sheet"]["facts"]] == ["F1", "F2"]
    assert r["dropped"] == 1
    assert "REPO_MAP:\nsrc/a.go\nsrc/b.go" in llm.calls["analyzeInputs"][0]
    assert "RELATED_CODE:\nFILE src/b.go" in llm.calls["analyzeInputs"][0]
    assert r["promptId"].startswith("analyze-code@")


def test_plan_docs_invented_fact_ids_are_removed_from_the_plan():
    r = plan_docs(fake_llm(), sheet={"facts": [{"id": "F1"}, {"id": "F2"}]}, brief="b", style_text="s", existing="", related=[], template="")
    assert r["plan"]["sections"][0]["must_cover"] == ["F1", "F2"]  # F99 dropped
    assert r["plan"]["gaps"] == ["How silences expire"]


def test_stage_retry_malformed_json_once_is_recovered_twice_is_an_error():
    n = {"i": 0}

    class Flaky:
        def chat_json(self, m, **kw):
            n["i"] += 1
            if n["i"] == 1:
                raise ValueError("bad json")
            return CODE_FACTS

    base = dict(page="p", style_key=None, changed_files=[], repo_map=[], code="", related=[])
    assert analyze_code(Flaky(), **base)["sheet"]["facts"]

    class Broken:
        def chat_json(self, m, **kw):
            raise ValueError("bad json")

    with pytest.raises(RuntimeError, match="invalid JSON twice"):
        analyze_code(Broken(), **base)


# ── graph: the two context stages ────────────────────────────────────────────
CODE_V1 = "### FILE: src/a.go\nfunc Alert() {}\n"
CODE_V2 = '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n'


def unit(**o):
    return {"kind": "code", "repo": "o/r", "filePath": "overview.md", "commit": "abc1234", "before": CODE_V1, "after": CODE_V2, "existing": "# Old\n", "changedFiles": ["src/a.go"],
            "styleKey": "API documentation", "brief": "Explain the alert API", "repoMap": ["src/a.go", "src/b.go"], "snapshotFiles": ["src/a.go"], **o}


def load_b(deps, llm=None, extra=None):
    tree = {"c0": {"src/b.go": "package main\nfunc Other() { Alert() }\n", **(extra or {})}}
    sync_context(repo="o/r", commit="c0", before="", git=Repo(tree), code_store=deps["codeVectors"], doc_store=deps["vectors"], llm=llm or deps["llm"], facts=deps["facts"])


def test_graph_code_mode_stage1_then_stage2_in_order_before_writing():
    deps = make_deps(llm=fake_llm([PASS_JUDGE]))
    load_b(deps, extra={"README.md": "# Proj\n\nAlert service docs.\n"})
    d = process_change(unit(), deps)
    assert [t["node"] for t in d["trail"]] == ["prefilter", "cross_repo", "route", "similarity", "code_context", "gar", "semantic_context", "write_draft", "verify_draft", "judge", "publish"]
    cc = next(t for t in d["trail"] if t["node"] == "code_context")["note"]
    assert cc["related"] >= 1, "pulled code from OUTSIDE the changed files"
    assert cc["facts"] == 2
    assert cc["droppedNoEvidence"] == 1
    sc = next(t for t in d["trail"] if t["node"] == "semantic_context")["note"]
    assert sc["related"] >= 1 and sc["sections"] == 2 and sc["gaps"] == 1
    draft_in = deps["llm"].calls["drafts"][0]
    assert re.search(r"FACT_SHEET:\n[\s\S]*The service listens on port 8081", draft_in)
    assert re.search(r"PLAN:\n[\s\S]*Configuration", draft_in)
    assert "RELATED_CODE:\nFILE src/b.go" in draft_in


def test_graph_code_mode_the_judge_sees_related_code_the_fact_sheet_and_the_plan():
    seen = []
    llm = fake_llm([PASS_JUDGE])
    orig = llm.chat_json

    def spy(m, **kw):
        if "You are the JUDGE" in m[0]["content"]:
            seen.append(m[1]["content"])
        return orig(m, **kw)

    llm.chat_json = spy
    deps = make_deps(llm=llm)
    load_b(deps)
    process_change(unit(), deps)
    assert "RELATED_CODE:\nFILE src/b.go" in seen[0]
    assert re.search(r"FACT_SHEET:\n[\s\S]*F1", seen[0])
    assert re.search(r"PLAN:\n[\s\S]*Overview", seen[0])


def test_graph_code_mode_a_widened_retry_rereads_the_whole_context_with_a_bigger_budget():
    deps = make_deps(llm=fake_llm([HALLUCINATION_JUDGE]), env={"MAX_ITERATIONS": "1"})
    d = process_change(unit(), deps)
    n = [t["node"] for t in d["trail"]]
    assert n[n.index("widen"):] == ["widen", "code_context", "gar", "semantic_context", "write_draft", "verify_draft", "judge", "fallback"]
    ccs = [t for t in d["trail"] if t["node"] == "code_context"]
    assert ccs[0]["note"]["topK"] == 12
    assert ccs[1]["note"]["topK"] == 30
    assert ccs[1]["note"]["widened"] is True
    assert deps["llm"].calls["analyze"] == 2
    assert deps["llm"].calls["plan"] == 2, "the plan is rebuilt from the larger context"
    assert [t for t in d["trail"] if t["node"] == "semantic_context"][1]["note"]["topK"] == 12


def test_graph_docs_mode_never_runs_the_llm_context_stages():
    deps = make_deps()
    d = process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc", "before": "## A\n\nx\n", "after": "## A\n\nx\n\n- one\n- two\n- three\n"}, deps)
    assert not any(t["node"] in ("code_context", "semantic_context") for t in d["trail"])
    assert deps["llm"].calls["analyze"] + deps["llm"].calls["plan"] == 0


def test_sync_context_honors_page_scopes_and_repo_level_excludes_excluded_files_never_reach_embedding():
    s = setup()
    seen = []
    orig = s["llm"].embed

    def spy(texts):
        seen.extend(texts)
        return orig(texts)

    s["llm"].embed = spy
    tree = {"c1": {
        "src/a.go": "package main\nfunc Alert() {}\n",
        "eval/fixtures/Work/notes.md": "---\nclassification: Restricted\n---\nconfidential note\n",
        "eval/fixtures/data.json": '{"confidential": true}',
        "k8s/generated.yaml": "kind: ConfigMap # generated copy of src\n",
        "docs/readme.md": "# Docs\n\npublic\n",
    }}
    pages = [{"path": "o.md", "scope": ["src/**", "k8s/**"]}]
    sync_context(repo="o/r", commit="c1", before="", git=Repo(tree), **s, pages=pages, scope=pages_scope(pages), exclude=["eval/**", "k8s/generated.yaml"])
    paths = [p["payload"]["path"] for p in s["code_store"].points.values()]
    assert list(dict.fromkeys(paths)) == ["src/a.go"]
    assert "confidential" not in "\n".join(seen), "restricted fixtures were never embedded"
    assert "generated copy" not in "\n".join(seen)
    assert pages_scope([{"path": "a.md"}]) == ["**"], "a page without scope means everything"


def test_build_code_changes_repo_level_exclude_is_applied_to_the_snapshot_sent_to_the_model():
    tree = {"c1": {"src/a.go": "a", "eval/x.md": "secret notes"}, "c2": {"src/a.go": "b", "eval/x.md": "secret notes 2"}}
    r = Repo(tree)
    units = build_code_changes(repo="o/r", policy={"exclude": ["eval/**"]}, commit="c2", before="c1", list_files=r.list_files, read_at=r.read_at,
                               changed_between=r.changed_between, read_existing_page=lambda p: "")
    assert "secret notes" not in units[0]["after"]
    assert "eval/x.md" not in units[0]["changedFiles"]


# ── deterministic page furniture ─────────────────────────────────────────────
def test_with_change_history_replaces_a_model_written_history_with_a_generated_truthful_one():
    draft = "## A\n\ntext\n\n## Change History\n\n| Date | Version |\n|---|---|\n| 2023-10-10 | 1.0 |\n"
    now = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    out = W.with_change_history(draft, repo="o/r", commit="abcdef1234567", gaps=["how silences expire"], now=now)
    assert "2023-10-10" not in out
    assert out.count("## Change History") == 1
    assert "| 2026-10-04 | abcdef1 | Documentation Bot | Generated from o/r at abcdef1 | Partially filled: the source does not show how silences expire |" in out
    assert "Fully filled" in W.with_change_history("## A\n\nx\n", repo="o/r", commit="abcdef1", now=now)
    # sections after the history survive
    assert re.search(r"## Appendix\n\nkeep[\s\S]*## Change History", W.with_change_history("## A\n\nx\n\n## Change History\n\nold\n\n## Appendix\n\nkeep\n", repo="o/r", commit="abcdef1"))


def test_with_frontmatter_an_explicit_title_and_description_override_the_guessed_ones():
    out = W.with_frontmatter("## Description\n\nfirst paragraph\n", file_path="overview.md", source_url="u", commit="c", title="rurag", description="What rurag is.")
    assert out.startswith('---\ntitle: "rurag"\ndescription: "What rurag is."')


def test_graph_code_mode_page_keeps_its_declared_path_gets_a_real_title_and_a_generated_change_history():
    draft = ("## Description\n\nA service that exposes A, B and C as its public operations, and does things well enough for a reader to use it without opening the code.\n\n"
             "## Usage\n\nCall A, B or C with the documented arguments.\n\n## Change History\n\n| 2023-10-10 | x |\n")
    deps = make_deps(policy={"trust": "review", "serviceName": "proj", "styleGuide": "", "glossary": {}}, llm=fake_llm([PASS_JUDGE], draft=draft))
    d = process_change({"kind": "code", "repo": "o/r", "filePath": "overview.md", "commit": "abc1234", "before": None,
                        "after": "### FILE: a.go\nfunc A() {}\nfunc B() {}\nfunc C() {}\n", "existing": "", "changedFiles": ["a.go"], "brief": "b"}, deps)
    assert d["subfolder"] is None, "no re-filing of a declared page"
    assert re.search(r"services/proj/overview\.md$", d["targetPath"])
    assert d["content"].startswith('---\ntitle: "proj"\n')
    assert re.search(r'description: "Explain the alert|Overview', d["content"], re.I)
    assert "2023-10-10" not in d["content"]
    assert re.search(r"Documentation Bot \| Generated from o/r at abc1234", d["content"])


def test_judge_note_records_which_facts_were_missing_for_the_audit_trail():
    docs = {"repo": "o/r", "filePath": "docs/a.md", "commit": "abc", "before": "## A\n\nx\n", "after": "## A\n\nx\n\n- one\n- two\n- three\n"}
    ok = process_change(docs, make_deps(llm=fake_llm([{**PASS_JUDGE, "facts": [{"text": "f1", "covered": True}, {"text": "the tools list", "covered": True}], "style": 0.9, "quality": 0.9}])))
    assert any(t["node"] == "judge" for t in ok["trail"])
    bad_judge = {"claims": [{"text": "c", "supported": False}], "facts": [{"text": "the tools list", "covered": False}], "style": 1, "quality": 1, "notes": []}
    bad = process_change(docs, make_deps(llm=fake_llm([bad_judge]), env={"MAX_ITERATIONS": "1"}))
    j = next(t for t in bad["trail"] if t["node"] == "judge")["note"]
    assert j["missing"] == ["the tools list"]
    assert j["unsupported"] == ["c"]


def test_critic_a_missing_core_fact_fails_even_when_the_overall_recall_ratio_is_high():
    t = make_deps()["cfg"]["thresholds"]
    m = {"precision": 1, "recall": 0.95, "coreRecall": 0.5, "missingCore": ["the nine MCP tools"], "missing": ["the nine MCP tools"], "style": 1, "quality": 1, "unsupported": [], "notes": []}
    r = evaluate(m, t)
    assert r["tag"] == "missing_core_fact"
    assert re.search(r"CORE fact.*nine MCP tools", r["feedback"][0])
    assert evaluate({**m, "coreRecall": 1, "missingCore": []}, t) is None


def test_judge_core_recall_is_computed_from_facts_flagged_core():
    class L:
        def chat_json(self, m, **kw):
            return {"claims": [], "facts": [{"text": "a", "covered": True, "core": True}, {"text": "b", "covered": False, "core": True}, {"text": "c", "covered": False}],
                    "style": 1, "quality": 1, "notes": []}

    v = judge(L(), mode="code", source="s", draft="d")
    assert v["coreRecall"] == 0.5
    assert v["missingCore"] == ["b"]
    assert round(v["recall"] * 100) == 33


# ── GAR proper: hypothetical docs from the fact sheet drive semantic retrieval ──
def test_gar_code_mode_paragraphs_come_from_the_fact_sheet_after_stage1_and_each_is_a_query_against_the_docs_index():
    llm = fake_llm([PASS_JUDGE])
    embedded = []
    orig = llm.embed

    def spy(texts):
        embedded.extend(texts)
        return orig(texts)

    llm.embed = spy
    d = process_change(unit(), make_deps(llm=llm))
    names = [t["node"] for t in d["trail"]]
    assert names.index("code_context") < names.index("gar"), "GAR runs after the fact sheet exists"
    gar = next(t for t in d["trail"] if t["node"] == "gar")["note"]
    assert gar["garParagraphs"] == 2, "blank paragraphs are dropped"
    assert llm.calls["garFacts"] == 1
    assert any(e.startswith("The service listens on a configurable port") for e in embedded)
    assert any(e.startswith("Alerts can be silenced by id") for e in embedded)
    sc = next(t for t in d["trail"] if t["node"] == "semantic_context")["note"]
    assert sc["garQueries"] == 2
    assert sc["queries"] == 5


def test_gar_hypothetical_text_is_never_written_to_either_index():
    deps = make_deps(policy={"trust": "auto", "serviceName": "p", "styleGuide": "", "glossary": {}})
    process_change(unit(), deps)
    all_text = "\n".join(p["payload"]["text"] for p in [*deps["vectors"].points.values(), *deps["codeVectors"].points.values()])
    assert "configurable port and is configured through environment variables" not in all_text


def test_gar_a_failure_only_degrades_retrieval_it_does_not_fail_the_run():
    llm = fake_llm([PASS_JUDGE])
    orig = llm.chat_json

    def spy(m, **kw):
        if "HYPOTHETICAL documentation" in m[0]["content"]:
            raise RuntimeError("boom")
        return orig(m, **kw)

    llm.chat_json = spy
    d = process_change(unit(), make_deps(llm=llm))
    assert d["outcome"] == "pending_review"
    gar = next(t for t in d["trail"] if t["node"] == "gar")["note"]
    assert gar["garParagraphs"] == 0
    assert gar["garError"] == "boom"


def test_gar_docs_mode_does_not_run_the_fact_sheet_gar():
    llm = fake_llm([PASS_JUDGE])
    d = process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc", "before": "## A\n\nx\n", "after": "## A\n\nx\n\n- one\n- two\n- three\n"}, make_deps(llm=llm))
    assert not llm.calls["garFacts"]
    assert next(t for t in d["trail"] if t["node"] == "gar")["note"]["garParagraphs"] == 0


# ── semantic sources, site docs, visibility, search ──────────────────────────
def test_sync_context_docs_globs_index_existing_documentation_as_semantic_context_exclude_still_wins():
    s = setup()
    tree = {"c1": {"src/a.go": "package main\nfunc A() {}\n", "apps/docs/guide/polls.mdx": "# Polls\n\nA poll lets people vote on times.\n",
                   "apps/docs/secret/internal.mdx": "# Internal\n\nrestricted\n", "CONTRIBUTING.md": "# Contributing\n\nopen a PR\n"}}
    sync_context(repo="o/r", commit="c1", before="", git=Repo(tree), **s, pages=[{"path": "o.md", "scope": ["src/**"]}], scope=["src/**"],
                 docs=["apps/docs/**/*.mdx", "CONTRIBUTING.md"], exclude=["apps/docs/secret/**"])
    doc_paths = [p["payload"]["path"] for p in s["doc_store"].points.values()]
    assert "apps/docs/guide/polls.mdx" in doc_paths and "CONTRIBUTING.md" in doc_paths
    assert not any("secret" in p for p in doc_paths), "excluded docs are never indexed"
    assert list(dict.fromkeys(p["payload"]["path"] for p in s["code_store"].points.values())) == ["src/a.go"], "docs do not leak into the code collection"


def test_sync_site_indexes_the_documentation_site_once_per_commit_under_its_own_key_and_skips_when_unchanged():
    s = setup()
    pages = {"src/content/docs/guides/a.md": "## Alpha\n\nAlpha explains the alert API.\n", "src/content/docs/guides/b.md": "## Beta\n\nBeta explains deployment.\n"}
    args = dict(site_repo="o/docs", commit="s1", files=list(pages), read_file=pages.get, doc_store=s["doc_store"], llm=s["llm"], facts=s["facts"])
    first = sync_site(**args)
    assert first["skipped"] is False
    assert first["pages"] == 2
    assert all(p["payload"]["repo"] == "site:o/docs" and p["payload"]["kind"] == "site_doc" for p in s["doc_store"].points.values())
    embeds = s["llm"].calls["embed"]
    assert sync_site(**args)["skipped"] is True
    assert s["llm"].calls["embed"] == embeds, "no re-embedding when the site commit is unchanged"
    sync_site(**{**args, "commit": "s2", "files": ["src/content/docs/guides/a.md"]})
    assert not any(p["payload"]["path"].endswith("b.md") for p in s["doc_store"].points.values()), "removed pages leave the index"


def test_retrieve_semantic_with_a_site_repo_also_returns_pages_from_the_rest_of_the_documentation_site():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s, pages=[{"path": "overview.md", "brief": "Explain alerting."}])
    sync_site(site_repo="o/docs", commit="s1", files=["src/content/docs/g.md"], read_file=lambda f: "## Alerting\n\nHow alerting works across the platform.\n",
              doc_store=s["doc_store"], llm=s["llm"], facts=s["facts"])
    kw = dict(llm=s["llm"], store=s["doc_store"], repo="o/r", queries=["alerting platform"], top_k=5)
    assert not any(h["kind"] == "site_doc" for h in retrieve_semantic(**kw))
    assert any(h["kind"] == "site_doc" and h["path"] == "src/content/docs/g.md" for h in retrieve_semantic(**kw, site_repo="o/docs"))


def test_site_files_reads_starlight_pages_and_skips_the_folders_the_pipeline_itself_writes(tmp_path):
    for f in ("guides/a.md", "ci-cd/b.mdx", "projects/x/overview.md", "services/y/z.md", "notes.txt"):
        p = tmp_path / "src/content/docs" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n")
    assert site_files(str(tmp_path)) == ["src/content/docs/ci-cd/b.mdx", "src/content/docs/guides/a.md"]


def test_context_search_and_status_make_the_database_inspectable():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s, pages=[{"path": "overview.md", "brief": "Explain alerting."}])
    sync_site(site_repo="o/docs", commit="s1", files=["p.md"], read_file=lambda f: "## P\n\ntext\n", doc_store=s["doc_store"], llm=s["llm"], facts=s["facts"])
    r = search_context(s["llm"], s["code_store"], s["doc_store"], "o/r", "func Alert", k=3)
    assert len(r["code"]) >= 1 and r["code"][0]["path"].startswith("src/")
    assert len(r["semantic"]) >= 1
    only = search_context(s["llm"], s["code_store"], s["doc_store"], "o/r", "func Alert", kind="code")
    assert "semantic" not in only
    rows = context_status(s["code_store"], s["doc_store"], s["facts"])
    repo_row = next(x for x in rows if x["repo"] == "o/r")
    assert repo_row["code"] == 2
    assert repo_row["source_doc"] >= 1
    assert repo_row["brief"] == 1
    assert next(x for x in rows if x["repo"] == "site:o/docs")["site_doc"] == 1


def test_graph_code_mode_the_decision_records_exactly_which_context_was_retrieved_and_which_gar_queries_were_used():
    deps = make_deps(llm=fake_llm([PASS_JUDGE]))
    load_b(deps, extra={"README.md": "# Proj\n\nAlert service docs.\n"})
    deps["siteRepo"] = "o/docs"
    d = process_change(unit(), deps)
    assert len(d["context"]["code"]) >= 1 and d["context"]["code"][0]["path"] == "src/b.go" and isinstance(d["context"]["code"][0]["score"], float)
    assert len(d["context"]["semantic"]) >= 1
    assert [f["id"] for f in d["context"]["facts"]] == ["F1", "F2"]
    assert len(d["context"]["gar"]) == 2
    assert len(d["context"]["plan"]) == 2
    assert next(t for t in d["trail"] if t["node"] == "code_context")["note"]["relatedTop"][0].startswith("src/b.go:")


# ── relevance floor, outlines, lean code-mode standards ──────────────────────
def test_retrieval_floor_marginal_matches_are_dropped_but_the_page_brief_is_always_kept():
    s = setup()
    sync_context(repo="o/r", commit="c1", before="", git=Repo(T), **s, pages=[{"path": "overview.md", "brief": "zzz qqq unrelated words"}])
    kw = dict(llm=s["llm"], repo="o/r", top_k=10)
    assert len(retrieve_code(store=s["code_store"], queries=["func Alert"], **kw)) >= 1
    assert len(retrieve_code(store=s["code_store"], queries=["func Alert"], min_score=0.999, **kw)) == 0
    sem = retrieve_semantic(store=s["doc_store"], queries=["completely different topic about bananas"], min_score=0.999, **kw)
    assert [x["kind"] for x in sem] == ["brief"], "only the brief survives a strict floor"


def test_code_mode_writes_to_the_style_outline_skips_template_selection_and_uses_lean_standards():
    llm = fake_llm([PASS_JUDGE])
    d = process_change(unit(styleKey="Data and schema reference"), make_deps(llm=llm))
    draft_in = llm.calls["drafts"][0]
    assert re.search(r"OUTLINE \(follow this order[^)]*\):\n## Overview\n## Entities", draft_in)
    assert "[Default Template]" not in draft_in, "the generic business template is not used in code mode"
    assert next(t for t in d["trail"] if t["node"] == "gar")["note"]["template"] == "outline:Data and schema reference"
    assert "TEMPLATE:\nOUTLINE" in llm.calls["planInputs"][0]


def test_docs_mode_is_unchanged_it_still_selects_and_applies_a_template():
    llm = fake_llm([PASS_JUDGE])
    d = process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc", "before": "## A\n\nx\n", "after": "## A\n\nx\n\n- one\n- two\n- three\n"}, make_deps(llm=llm))
    assert "default-template" in next(t for t in d["trail"] if t["node"] == "gar")["note"]["template"]


def test_every_bundled_style_has_an_outline_and_the_lean_standards_forbid_the_sections_that_broke_the_first_run():
    for k, v in load_styles()["styles"].items():
        assert isinstance(v.get("outline"), list) and len(v["outline"]) >= 1, f"{k} outline"
    assert "Do not write a Change History section, a Classification section, a References section" in load_prompt("standards-code")["text"]


def test_judge_prompts_tell_the_model_to_ignore_statements_about_the_document_itself():
    for n in ("judge-docs", "judge-code"):
        assert "Statements about the document itself" in load_prompt(n)["text"]


def test_the_decision_records_which_files_the_page_was_based_on():
    deps = make_deps(llm=fake_llm([PASS_JUDGE]))
    d = process_change(unit(snapshotFiles=["src/a.go"], repoMap=["src/a.go", "src/b.go", "src/c.go"]), deps)
    assert d["context"]["snapshot"] == {"files": ["src/a.go"], "count": 1, "chars": len(unit()["after"]), "scoped": 3}
    note = next(t for t in d["trail"] if t["node"] == "code_context")["note"]
    assert note["basedOnFiles"] == 1
    assert note["scopedFiles"] == 3


# ── deterministic completeness ───────────────────────────────────────────────
PRISMA = "model User {\n  id String\n}\nmodel Poll {\n  id String\n}\nenum PollStatus {\n  open\n}\nmodel Vote {\n  id String\n}\n"


def test_coverage_finds_every_declared_model_enum_and_reports_what_the_page_omits():
    spec = {"kinds": ["prisma_model", "prisma_enum"], "min": 0.9}
    c = check_coverage("### User\n\n### Poll\n\nUses `PollStatus`.", PRISMA, spec)
    assert c["kinds"] == {"prisma_model": {"total": 3, "found": 2}, "prisma_enum": {"total": 1, "found": 1}}
    # the same name matched by two extractors counts once
    assert check_coverage("User", PRISMA, {"kinds": ["prisma_enum", "graphql_type"], "min": 1})["kinds"]["graphql_type"]["total"] == 0
    assert c["missing"] == ["prisma_model:Vote"]
    assert c["ok"] is False
    assert check_coverage("User Poll Vote PollStatus", PRISMA, spec)["ok"] is True


def test_coverage_whole_word_matching_and_no_declarations_means_nothing_to_enforce():
    assert check_coverage("Voted and Poller", PRISMA, {"kinds": ["prisma_model"], "min": 1})["kinds"]["prisma_model"]["found"] == 0
    assert check_coverage("anything", "no models here", {"kinds": ["prisma_model"]}) is None
    assert check_coverage("x", PRISMA, None) is None
    assert check_coverage("API_KEY", "const a = process.env.API_KEY; const b = process.env.DATABASE_URL;", {"kinds": ["env_var"], "min": 0.5})["missing"] == ["env_var:DATABASE_URL"]


MANY = [f"M{i + 1}" for i in range(10)]
MODELS = "### FILE: schema.prisma\n" + "\n".join(f"model {m} {{\n  id String\n}}" for m in MANY) + "\n"


def schema_draft(names):
    body = "\n".join(f"### {n}\n\nThe {n} entity.\n" for n in names)
    return ("## Overview\n\nThe database layer stores every entity of the application in one relational schema and this page documents them.\n\n"
            f"## Entities\n\n{body}")


SCHEMA = dict(styleKey="Data and schema reference", changedFiles=["schema.prisma"])


def test_graph_a_schema_page_naming_most_but_not_all_models_passes_verifier_and_is_caught_by_coverage_with_exact_names_fed_back():
    llm = fake_llm([PASS_JUDGE], draft=schema_draft(MANY[:7]))
    d = process_change(unit(after=MODELS, before=None, **SCHEMA), make_deps(llm=llm, env={"MAX_ITERATIONS": "2"}))
    assert d["outcome"] == "fallback"
    j = [t for t in d["trail"] if t["node"] == "judge"]
    assert j[0]["note"]["failure"] == "incomplete_coverage"
    assert j[0]["note"]["coverage"]["prisma_model"] == "7/10"
    assert j[0]["note"]["coverageMissing"] == ["prisma_model:M8", "prisma_model:M9", "prisma_model:M10"]
    assert "Add every one of: M8, M9, M10" in llm.calls["drafts"][1], "the next attempt is told exactly what is missing"


def test_graph_a_draft_that_names_too_few_symbols_never_reaches_the_judge():
    llm = fake_llm([PASS_JUDGE], draft=schema_draft(MANY[:3]))
    d = process_change(unit(after=MODELS, before=None, **SCHEMA), make_deps(llm=llm, env={"MAX_ITERATIONS": "2"}))
    assert llm.calls["judge"] == 0, "no judge call was spent on an obviously incomplete draft"
    v = next(t for t in d["trail"] if t["node"] == "verify_draft")
    assert v["note"]["ok"] is False
    assert "Name each of them: M10, M4, M5" in v["note"]["reasons"][0]


def test_graph_when_the_page_names_every_declared_model_verification_and_coverage_pass_and_the_page_publishes():
    llm = fake_llm([PASS_JUDGE], draft=schema_draft(MANY))
    d = process_change(unit(after=MODELS, before=None, **SCHEMA), make_deps(llm=llm))
    assert d["outcome"] == "pending_review"
    assert next(t for t in d["trail"] if t["node"] == "judge")["note"]["coverage"]["prisma_model"] == "10/10"


# ── regressions found by the first flare-optimizer-v2 run ────────────────────
def test_judge_retries_once_when_the_reply_is_not_valid_json_and_a_second_failure_still_raises():
    seen = {"n": 0}

    class Flaky:
        def chat_json(self, m, **kw):
            seen["n"] += 1
            if seen["n"] == 1:
                raise ValueError("Expecting ',' delimiter")
            assert "not valid JSON" in m[-1]["content"]
            return PASS_JUDGE

    assert judge(Flaky(), mode="code", source="s", draft="d")["precision"] == 1
    assert seen["n"] == 2

    class Broken:
        def chat_json(self, m, **kw):
            raise ValueError("bad")

    with pytest.raises(ValueError):
        judge(Broken(), mode="code", source="s", draft="d")


API_SRC = ('### FILE: app/main.py\n@app.post("/start-training/")\ndef handle_train_request(): pass\n@app.post("/start-evaluation/")\ndef handle_eval_request(): pass\n'
           'def setup_logging(): pass\ndef debug_message(): pass\nclass Settings: pass\nx = os.environ["APPLICATION_NAME"]\n')


def test_a_new_page_must_name_its_routes_not_every_helper_function_and_environment_variable():
    draft = ("## Overview\n\nThe service starts a training run with POST /start-training/ and an evaluation run with POST /start-evaluation/, each with a JSON body.\n\n"
             "## Usage\n\nCall the training route first, then the evaluation route once the model is stored.\n")
    llm = fake_llm([PASS_JUDGE], draft=draft)
    d = process_change({"kind": "code", "repo": "o/r", "filePath": "api.md", "commit": "abc1234", "before": None, "after": API_SRC, "existing": "", "changedFiles": ["app/main.py"],
                        "styleKey": "API documentation"}, make_deps(llm=llm))
    v = next(t for t in d["trail"] if t["node"] == "verify_draft")["note"]
    assert v["ok"] is True and v["mentioned"] == "2/2", v
    assert d["outcome"] == "pending_review"
