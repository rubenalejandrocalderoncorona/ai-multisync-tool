import pytest

from multisync.factstore import MemoryFactStore
from multisync.pipeline import process_change
from multisync.repofacts import (SourceReader, checked_repo_facts, conflicts, deterministic_facts, deterministic_signals, facts_text, is_stale, known_fact_lines,
                                 llm_facts, profile_repo)
from multisync.symbols import extract_public_symbols
from multisync.testing import PASS_JUDGE, fake_llm, make_deps

README = ("# SuperGit\n\nA dual-interface GitHub repository browser and analytics tool, GUI and TUI sharing a single Go REST API backend.\n\n"
          "## Run\n\nRequires Go 1.22+ and Node 18. make web starts the API and the GUI.\n")
BASE = {
    "README.md": README, "Makefile": "install:\n\tnpm i\nweb:\n\techo\nstop:\n\techo\n",
    "apps/tui/go.mod": "module example.com/supergit\n\ngo 1.24.2\n\nrequire (\n\tgithub.com/charmbracelet/bubbletea v1\n\tgithub.com/google/go-github/v68 v68\n)\n",
    "apps/tui/cmd/server/main.go": "package main\nfunc main() {}\n",
    "apps/tui/internal/api/h.go": 'package api\nfunc Register() {\n\tmux.HandleFunc("GET /api/health", h)\n\tmux.HandleFunc("POST /api/users", h)\n}\nvar _ = os.Getenv("PORT_X")\n' + "// pad\n" * 40,
    "apps/web/package.json": '{"name":"web","scripts":{"dev":"next dev","build":"next build"},"dependencies":{"next":"^16.0.1","react":"19"},"devDependencies":{"tailwindcss":"4"}}',
    "apps/web/src/app/page.tsx": "export default function Page() { return null }\n" * 30,
    ".github/workflows/ci.yml": "name: ci\n",
}


class Git:
    def __init__(self, tree):
        self.tree = tree

    def list_files(self, rev):
        return list(self.tree)

    def read_at(self, rev, f):
        return self.tree.get(f)


def tree(**over):
    return {**BASE, **over}


def by_cat(rows):
    return {r["category"]: r["fact"] for r in rows}


def test_go_http_routes_are_public_interface():
    names = [s["name"] for s in extract_public_symbols("h.go", BASE["apps/tui/internal/api/h.go"]) if s["kind"] == "route"]
    assert names == ["GET /api/health", "POST /api/users"]


def test_deterministic_facts_cover_language_stack_build_ci_api_and_config_and_name_their_source():
    rows = deterministic_facts("o/SuperGit", list(BASE), BASE.get)
    f = by_cat(rows)
    assert f["language"].startswith("SuperGit is mainly written in TypeScript") and "Go (24%)" in f["language"], f["language"]
    stack = " | ".join(r["fact"] for r in rows if r["category"] == "stack")
    assert "Go module example.com/supergit for Go 1.24.2" in stack and "Bubble Tea" in stack
    assert "Next.js, React and Tailwind CSS" in stack and "npm scripts: dev, build" in stack
    assert any("Makefile defines the targets install, web and stop" in r["fact"] for r in rows)
    assert "ci.yml" in f["ci"] and "2 HTTP route(s)" in f["api"] and "PORT_X" in f["config"]
    paths = {r["category"]: r["source_path"] for r in rows if r["category"] != "stack"}
    assert paths["language"] == "@source-files" and paths["ci"] == "@workflows" and paths["build"] in ("@entrypoints", "Makefile")
    assert {r["source_path"] for r in rows if r["category"] == "stack"} == {"apps/tui/go.mod", "apps/web/package.json"}
    assert all(r["verification_method"] == "deterministic" for r in rows)


class RepoFactsLLM:
    def __init__(self, facts):
        self.facts, self.calls, self.sections = facts, 0, []

    def chat_json(self, messages, **kw):
        self.calls += 1
        self.sections.append(messages[1]["content"])
        assert kw.get("tier") == "cheap"
        return {"facts": self.facts}


PURPOSE = {"category": "purpose", "fact": "SuperGit is a GitHub repository browser.", "evidence": "A dual-interface GitHub repository browser and analytics tool"}


def test_llm_facts_need_a_verbatim_quote_and_are_attributed_to_the_file_that_contains_it():
    llm = RepoFactsLLM([PURPOSE, {"category": "feature", "fact": "It supports Kubernetes.", "evidence": "native Kubernetes operator"},
                        {"category": "purpose", "fact": "No quote at all.", "evidence": ""},
                        {"category": "weird", "fact": "Whitespace and case are tolerated.", "evidence": "gui  and TUI sharing\na single Go REST API"}])
    kept, dropped = llm_facts(llm, {"README.md": README})
    assert [k["fact"] for k in kept] == ["SuperGit is a GitHub repository browser.", "Whitespace and case are tolerated."]
    assert dropped == 2 and kept[1]["category"] == "purpose"
    assert {k["source_path"] for k in kept} == {"README.md"} and {k["verification_method"] for k in kept} == {"llm_quote_grounded"}


def test_profile_records_source_hash_and_extraction_time_for_every_fact():
    facts = MemoryFactStore()
    profile_repo("o/SuperGit", "c1", Git(BASE), facts, RepoFactsLLM([PURPOSE]))
    rows = facts.repo_facts("o/SuperGit")
    assert {r["source"] for r in rows} == {"deterministic", "llm"}
    assert all(len(r["source_hash"]) == 64 and r["extracted_at"] and r["source_path"] for r in rows)
    readme = next(r for r in rows if r["source"] == "llm")
    assert readme["source_path"] == "README.md" and readme["source_hash"] == SourceReader(Git(BASE), "c1").digest("README.md")


def test_the_model_is_skipped_while_the_readme_is_unchanged_and_runs_again_when_it_changes():
    facts, llm = MemoryFactStore(), RepoFactsLLM([PURPOSE])
    assert profile_repo("o/S", "c1", Git(BASE), facts, llm)["llm"] == 1
    again = profile_repo("o/S", "c2", Git(BASE), facts, llm)
    assert again["llmSkipped"] is True and llm.calls == 1, "same README, no second model call"
    changed = tree(**{"README.md": README + "\nMore text.\n"})
    assert profile_repo("o/S", "c3", Git(changed), facts, llm)["llmSkipped"] is False and llm.calls == 2


def test_staleness_is_a_hash_compare_per_fact():
    facts = MemoryFactStore()
    profile_repo("o/S", "c1", Git(BASE), facts, RepoFactsLLM([PURPOSE]))
    rows = facts.repo_facts("o/S")
    fresh = SourceReader(Git(BASE), "c1")
    assert not any(is_stale(r, fresh) for r in rows)
    new = SourceReader(Git(tree(**{"README.md": README + "x", "Makefile": "other:\n"})), "c2")
    stale = {r["source_path"] for r in rows if is_stale(r, new)}
    assert stale == {"README.md", "Makefile"}
    assert is_stale({"source_path": "go.mod", "source_hash": ""}, fresh), "a fact without provenance counts as stale"
    assert is_stale({"source_path": "gone.md", "source_hash": "abc"}, fresh)


def test_checked_repo_facts_rebuilds_stale_facts_inline_and_only_the_stale_readme_is_sent_to_the_model():
    facts, llm = MemoryFactStore(), RepoFactsLLM([PURPOSE])
    profile_repo("o/S", "c1", Git(BASE), facts, llm)
    assert llm.calls == 1
    rows, rep = checked_repo_facts(facts, "o/S", "c1", Git(BASE), llm)
    assert rep["stale"] == 0 and not rep["rebuilt"] and llm.calls == 1, "nothing changed: no rebuild, no model call"
    moved = tree(**{"README.md": README.replace("browser", "viewer"), "Makefile": "install:\n\tnpm i\n"})
    llm.facts = [{"category": "purpose", "fact": "SuperGit is a viewer.", "evidence": "dual-interface GitHub repository viewer"}]
    rows, rep = checked_repo_facts(facts, "o/S", "c2", Git(moved), llm)
    assert rep["stale"] >= 2 and rep["rebuilt"] and rep["excluded"] == 0 and llm.calls == 2
    assert "SuperGit is a viewer." in [r["fact"] for r in rows] and "SuperGit is a GitHub repository browser." not in [r["fact"] for r in rows]
    assert "README.md" in llm.sections[1] and "Makefile" not in llm.sections[1]
    assert any("Makefile defines the targets install." in r["fact"] for r in rows), "deterministic facts are rebuilt too"


def test_without_a_model_stale_readme_facts_are_left_out_not_trusted():
    facts = MemoryFactStore()
    profile_repo("o/S", "c1", Git(BASE), facts, RepoFactsLLM([PURPOSE]))
    rows, rep = checked_repo_facts(facts, "o/S", "c2", Git(tree(**{"README.md": README + "changed"})), None)
    assert rep["excluded"] == 1 and not any(r["source"] == "llm" for r in rows)


def test_a_readme_fact_that_contradicts_a_deterministic_source_is_flagged():
    sig = deterministic_signals(list(BASE), BASE.get)
    assert sig["go"] == "1.24.2" and sig["frameworks"]["Next.js"] == "16" and sig["top_language"] in ("TypeScript", "Go")
    go = {"fact": "It requires Go 1.22 or newer.", "evidence": "Requires Go 1.22+ and Node 18", "source": "llm"}
    assert "go.mod declares Go 1.24.2" in conflicts(go, sig)
    assert conflicts({"fact": "Requires Go 1.24.", "evidence": "Go 1.24", "source": "llm"}, sig) is None
    lang = {"fact": "SuperGit is mainly written in Go.", "evidence": "mainly written in Go", "source": "llm"}
    top = sig["top_language"]
    assert (conflicts(lang, sig) is None) == (top == "Go")
    assert "package.json depends on Next.js 16" in conflicts({"fact": "The GUI is built with Next.js 14.", "evidence": "Next.js 14", "source": "llm"}, sig)
    assert conflicts({"fact": "It is a repository browser.", "evidence": "repository browser", "source": "llm"}, sig) is None, "no deterministic counterpart: nothing to reconcile"


def test_profile_flags_conflicts_and_they_reach_the_planner_and_the_judge_as_explicit_conflicts():
    facts = MemoryFactStore()
    llm = RepoFactsLLM([PURPOSE, {"category": "usage", "fact": "SuperGit requires Go 1.22 or newer.", "evidence": "Requires Go 1.22+ and Node 18"}])
    st = profile_repo("o/S", "c1", Git(BASE), facts, llm)
    assert st["conflicts"] == 1
    rows = facts.repo_facts("o/S")
    bad = next(r for r in rows if r.get("flag"))
    assert bad["flag"] == "contradicts_deterministic_source" and "Go 1.24.2" in bad["flag_detail"]
    assert "CONFLICT, do not state: \"SuperGit requires Go 1.22 or newer.\"" in facts_text(rows)
    assert any(l.startswith("CONFLICT: the README-derived fact") and "Go 1.24.2" in l for l in known_fact_lines(rows))
    assert "SuperGit is a GitHub repository browser." in known_fact_lines(rows), "unflagged facts stay plain"


CODE = {"kind": "code", "repo": "o/S", "filePath": "overview.md", "commit": "c1", "before": "### FILE: src/a.go\nfunc Alert() {}\n", "existing": "# Old\n",
        "after": '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n', "changedFiles": ["a.go"], "styleKey": "API documentation"}


def test_the_pipeline_rebuilds_stale_facts_before_planning_and_hands_conflicts_to_the_judge():
    llm = fake_llm([PASS_JUDGE], repo_facts=[PURPOSE, {"category": "usage", "fact": "SuperGit requires Go 1.22 or newer.", "evidence": "Requires Go 1.22+ and Node 18"}])
    deps = make_deps(llm=llm)
    old = Git(tree(**{"README.md": README + "old edition"}))
    profile_repo("o/S", "c1", old, deps["facts"], llm)  # facts extracted from an older README
    calls_before = llm.calls["log"].count({"kind": "repo-facts", "tier": "cheap"})
    deps["repoGit"] = Git(BASE)  # the README has moved on since
    d = process_change(CODE, deps)
    note = next(t for t in d["trail"] if t["node"] == "gar")["note"]["repoFacts"]
    assert note["stale"] >= 1 and note["rebuilt"] is True and note["conflicts"] == 1
    assert llm.calls["log"].count({"kind": "repo-facts", "tier": "cheap"}) == calls_before + 1
    assert "CONFLICT, do not state" in llm.calls["planInputs"][0] and "Go 1.24.2" in llm.calls["planInputs"][0]
    assert d["outcome"] == "pending_review"


def test_facts_stored_before_provenance_existed_are_purged_on_the_next_profile():
    facts = MemoryFactStore()
    facts.add_repo_facts("o/S", [{"category": "purpose", "fact": "legacy fact", "evidence": "x", "source": "llm"}], "c0")  # no source_path, no hash
    profile_repo("o/S", "c1", Git(BASE), facts, RepoFactsLLM([PURPOSE]))
    assert "legacy fact" not in [r["fact"] for r in facts.repo_facts("o/S")]
