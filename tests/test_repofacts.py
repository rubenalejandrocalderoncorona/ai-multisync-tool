from multisync.factstore import MemoryFactStore
from multisync.repofacts import deterministic_facts, llm_facts, profile_repo
from multisync.symbols import extract_public_symbols

README = "# SuperGit\n\nA dual-interface GitHub repository browser and analytics tool, GUI and TUI sharing a single Go REST API backend.\n\n## Run\n\nmake web starts the API and the GUI.\n"
TREE = {
    "README.md": README, "Makefile": "install:\n\tnpm i\nweb:\n\techo\nstop:\n\techo\n",
    "apps/tui/go.mod": "module example.com/supergit\n\ngo 1.22\n\nrequire (\n\tgithub.com/charmbracelet/bubbletea v1\n\tgithub.com/google/go-github/v68 v68\n)\n",
    "apps/tui/cmd/server/main.go": "package main\nfunc main() {}\n",
    "apps/tui/internal/api/h.go": 'package api\nfunc Register() {\n\tmux.HandleFunc("GET /api/health", h)\n\tmux.HandleFunc("POST /api/users", h)\n}\nvar _ = os.Getenv("PORT_X")\n' + "// pad\n" * 40,
    "apps/web/package.json": '{"name":"web","scripts":{"dev":"next dev","build":"next build"},"dependencies":{"next":"16","react":"19"},"devDependencies":{"tailwindcss":"4"}}',
    "apps/web/src/app/page.tsx": "export default function Page() { return null }\n",
    ".github/workflows/ci.yml": "name: ci\n",
}


class Git:
    def list_files(self, rev):
        return list(TREE)

    def read_at(self, rev, f):
        return TREE.get(f)


def by_cat(rows):
    return {r["category"]: r["fact"] for r in rows}


def test_go_http_routes_are_public_interface():
    names = [s["name"] for s in extract_public_symbols("h.go", TREE["apps/tui/internal/api/h.go"]) if s["kind"] == "route"]
    assert names == ["GET /api/health", "POST /api/users"]


def test_deterministic_facts_cover_language_stack_build_ci_api_and_config():
    rows = deterministic_facts("o/SuperGit", list(TREE), TREE.get)
    f = by_cat(rows)
    assert f["language"].startswith("SuperGit is mainly written in Go"), f["language"]
    assert "TypeScript" in f["language"]
    stack = " | ".join(r["fact"] for r in rows if r["category"] == "stack")
    assert "Go module example.com/supergit for Go 1.22" in stack and "Bubble Tea" in stack
    assert "Next.js, React and Tailwind CSS" in stack and "npm scripts: dev, build" in stack
    assert "apps/tui/cmd/server/main.go" in f["build"] or any("cmd/server/main.go" in r["fact"] for r in rows)
    assert any("Makefile defines the targets install, web and stop" in r["fact"] for r in rows)
    assert "ci.yml" in f["ci"]
    assert "2 HTTP route(s)" in f["api"] and "GET /api/health" in f["api"]
    assert "PORT_X" in f["config"]
    assert all(r["evidence"] for r in rows)


class FakeLLM:
    def __init__(self, facts):
        self.facts, self.calls = facts, 0

    def chat_json(self, messages, **kw):
        self.calls += 1
        assert kw.get("tier") == "cheap"
        return {"facts": self.facts}


def test_llm_facts_need_a_verbatim_quote_from_the_source():
    llm = FakeLLM([
        {"category": "purpose", "fact": "SuperGit is a GitHub repository browser.", "evidence": "A dual-interface GitHub repository browser and analytics tool"},
        {"category": "feature", "fact": "It supports Kubernetes.", "evidence": "native Kubernetes operator"},
        {"category": "purpose", "fact": "No quote at all.", "evidence": ""},
        {"category": "weird", "fact": "Whitespace and case are tolerated.", "evidence": "gui  and TUI sharing\na single Go REST API"},
    ])
    kept, dropped = llm_facts(llm, "### FILE: README.md\n" + README)
    assert [k["fact"] for k in kept] == ["SuperGit is a GitHub repository browser.", "Whitespace and case are tolerated."]
    assert dropped == 2
    assert kept[1]["category"] == "purpose"


def test_profile_stores_both_kinds_and_skips_the_model_when_the_readme_is_unchanged():
    facts, llm = MemoryFactStore(), FakeLLM([{"category": "purpose", "fact": "A repository browser.", "evidence": "GitHub repository browser and analytics tool"}])
    s1 = profile_repo("o/SuperGit", "c1", Git(), facts, llm)
    assert s1["deterministic"] >= 6 and s1["llm"] == 1 and not s1["llmSkipped"]
    assert {r["source"] for r in facts.repo_facts("o/SuperGit")} == {"deterministic", "llm"}
    s2 = profile_repo("o/SuperGit", "c2", Git(), facts, llm)
    assert s2["llmSkipped"] is True and llm.calls == 1, "same README, no second model call"
    TREE["README.md"] = README + "\nMore text.\n"
    try:
        assert profile_repo("o/SuperGit", "c3", Git(), facts, llm)["llmSkipped"] is False and llm.calls == 2
    finally:
        TREE["README.md"] = README
    assert profile_repo("o/SuperGit", "c4", Git(), MemoryFactStore(), None)["llmSkipped"] is True


def test_run_views_list_runs_and_show_their_stages():
    f = MemoryFactStore()
    for node, status, usd in [("prefilter", "ok", 0), ("write_draft", "ok", 0.05), ("judge", "error", 0.02)]:
        f.record_node_log({"runId": "r1", "repo": "o/a", "path": "p.md", "commit": "c", "node": node, "status": status, "ms": 5, "note": {"usage": {"expensive": {"usd": usd}}}, "at": "2026-01-01T00:00:00Z"})
    f.record_decision({"runId": "r1", "repo": "o/a", "outcome": "fallback"})
    r = f.list_runs()[0]
    assert (r["run_id"], r["nodes"], r["errors"], r["outcomes"]) == ("r1", 3, 1, {"fallback": 1}) and abs(r["usd"] - 0.07) < 1e-9
    assert [x["node"] for x in f.run_logs("r1")] == ["prefilter", "write_draft", "judge"]
    assert f.list_runs(repo="o/none") == []
