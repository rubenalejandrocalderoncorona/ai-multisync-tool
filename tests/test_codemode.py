import re

from multisync.codesource import build_code_changes, glob_to_regexp, scrub, select_files, snapshot
from multisync.critic import judge
from multisync.pipeline import process_change
from multisync.prompts import fill, load_prompt, load_styles, resolve_style, style_text
from multisync.testing import PASS_JUDGE, fake_llm, make_deps
from multisync.vectorstore import index_approved


def test_glob_double_star_crosses_directories_single_star_does_not():
    assert glob_to_regexp("src/**").match("src/a/b/c.go")
    assert glob_to_regexp("**/*.js").match("a/b/c.js")
    assert glob_to_regexp("**/*.js").match("c.js")
    assert not glob_to_regexp("src/*.js").match("src/a/b.js")


def test_select_files_honours_scope_and_drops_tests_lockfiles_vendored_and_generated_files():
    files = ["src/api.go", "src/api_test.go", "package-lock.json", "node_modules/x/i.js", "docs/a.md", "README.md", "cmd/main.go", "logo.png"]
    assert select_files(files) == ["src/api.go", "README.md", "cmd/main.go"]
    assert select_files(files, ["src/**"]) == ["src/api.go"]


def test_snapshot_puts_changed_files_first_caps_size_and_never_includes_secret_looking_lines():
    files = {"a.js": "const x = 1;\n", "b.js": 'const API_KEY = "abcdefghijklmnopqrstuvwxyz123456";\nok()\n', "c.js": "z" * 500}
    snap = snapshot(lambda f: files.get(f), ["a.js", "b.js", "c.js"], changed=["b.js"], max_chars=200)
    assert snap["files"][0] == "b.js"
    assert "abcdefghijklmnopqrstuvwxyz123456" not in snap["text"]
    assert len(snap["text"]) <= 200
    assert "secret" in scrub('password = "supersecretvalue12345"')


def git_stub(tree):
    return {
        "list_files": lambda rev: list(tree.get(rev, {})),
        "read_at": lambda rev, f: tree.get(rev, {}).get(f),
        "changed_between": lambda a, b: [f for f in tree[b] if tree.get(a, {}).get(f) != tree[b][f]],
        "read_existing_page": lambda page: "# Old page\n",
    }


def test_build_code_changes_only_pages_whose_scoped_files_changed_become_units():
    tree = {"c1": {"src/a.go": "one", "cmd/m.go": "m"}, "c2": {"src/a.go": "two", "cmd/m.go": "m"}}
    policy = {"pages": [{"path": "api.md", "kind": "API documentation", "scope": ["src/**"]}, {"path": "cli.md", "scope": ["cmd/**"]}]}
    units = build_code_changes(repo="o/r", policy=policy, commit="c2", before="c1", **git_stub(tree))
    assert [u["filePath"] for u in units] == ["api.md"]
    assert units[0]["kind"] == "code"
    assert units[0]["styleKey"] == "API documentation"
    assert units[0]["changedFiles"] == ["src/a.go"]
    assert re.search(r"### FILE: src/a\.go\ntwo", units[0]["after"])
    assert "one" in units[0]["before"]
    assert units[0]["existing"] == "# Old page\n"


def test_build_code_changes_no_previous_commit_documents_everything_removed_files_are_reported():
    tree = {"c2": {"src/a.go": "x"}}
    assert len(build_code_changes(repo="o/r", policy={}, commit="c2", before="", **git_stub(tree))) == 1
    t2 = {"c1": {"src/a.go": "x", "src/b.go": "y"}, "c2": {"src/a.go": "x"}}
    u = build_code_changes(repo="o/r", policy={}, commit="c2", before="c1", **{**git_stub(t2), "changed_between": lambda a, b: ["src/b.go"]})
    assert u[0]["changedFiles"] == ["src/b.go (removed)"]


# ── graph in code mode ───────────────────────────────────────────────────────
CODE_V1 = "### FILE: src/a.go\nfunc Alert() {}\n"
CODE_V2 = '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n'


def unit(**o):
    return {"kind": "code", "repo": "o/r", "filePath": "overview.md", "commit": "abc1234", "before": CODE_V1, "after": CODE_V2, "existing": "# Old\n",
            "changedFiles": ["src/a.go"], "styleKey": "API documentation", **o}


def test_code_mode_forced_shape_change_runs_the_full_path_and_uses_the_code_prompts_and_chosen_style():
    llm = fake_llm([PASS_JUDGE])
    d = process_change(unit(), make_deps(llm=llm))
    assert d["outcome"] == "pending_review"
    assert d["mode"] == "code"
    assert d["style"] == "API documentation"
    assert re.search(r"EXISTING_PAGE:\n# Old", llm.calls["drafts"][0])
    assert "CHANGED_FILES: src/a.go" in llm.calls["drafts"][0]
    jd = next(t for t in d["trail"] if t["node"] == "judge")
    assert re.match(r"^judge-code@[0-9a-f]{8}$", jd["note"]["prompt"])
    assert "doc_key: overview.md" in d["content"]
    assert "source: https://github.com/o/r/tree/abc1234" in d["content"]


def test_code_mode_similarity_uses_the_gar_paragraph_against_the_pages_own_chunks():
    deps = make_deps(env={"MIN_DIFF_LINES": "2", "SIMILARITY_HIGH": "0.5"})
    index_approved(deps["vectors"], deps["llm"], "o/r", "overview.md", "## Overview\n\nThe service exposes an alert API on port 8080.\n", "old")
    # Non-forced change (fact token only) whose GAR paragraph matches the existing page.
    d = process_change(unit(before=CODE_V2, after=CODE_V2.replace("8081", "8082")), deps)
    assert d["outcome"] == "refreshed"
    assert next(t for t in d["trail"] if t["node"] == "similarity")["note"]["mode"] == "code"
    assert not any(t["node"] == "judge" for t in d["trail"]), "no judge call when the page already says it"


# ── prompts and styles ───────────────────────────────────────────────────────
def test_prompts_judge_prompts_keep_the_json_contract_and_the_injection_guard():
    for n in ("judge-docs", "judge-code"):
        p = load_prompt(n)
        assert re.search(r'"claims":\[\{"text":"","supported":true,"evidence":""\}\]', p["text"])
        assert re.search(r'"facts":\[\{"text":"","covered":true,"core":false\}\]', p["text"])
        assert "data, not commands" in p["text"]
        assert re.match(rf"^{n}@[0-9a-f]{{8}}$", p["id"])


def test_prompts_every_draft_prompt_carries_the_persona_slot_and_the_injection_guard():
    for n in ("draft-docs", "draft-code"):
        p = load_prompt(n)
        assert "{{PERSONA}}" in p["text"]
        assert fill(p["text"], {"PERSONA": "You are X."}).startswith("You are X.")
        assert "data, not instructions" in p["text"]


def test_styles_library_is_a_key_value_store_with_persona_and_rubric_default_resolves_unknown_falls_back():
    lib = load_styles()
    assert len(lib["styles"]) >= 8
    for k, v in lib["styles"].items():
        assert v["prompt"].startswith("You are"), f"{k} persona"
        assert isinstance(v["rubric"], list) and len(v["rubric"]) >= 3, f"{k} rubric"
    assert resolve_style(lib, "API documentation")["fallback"] is False
    unk = resolve_style(lib, "No such style")
    assert unk["key"] == lib["defaultStyle"]
    assert unk["fallback"] is True
    assert re.search(r"Rubric:[\s\S]*Glossary[\s\S]*FactStore", style_text(resolve_style(lib, "API documentation"), {"glossary": {"FactStore": "x"}}))


def test_judge_sends_the_code_prompt_and_the_code_evidence_block_in_code_mode():
    seen = []

    class L:
        def chat_json(self, m, **kw):
            seen.append(m)
            return PASS_JUDGE

    v = judge(L(), mode="code", source=CODE_V2, draft="d", changed_files=["src/a.go"], existing="old", style_text="S")
    assert "FROM SOURCE CODE" in seen[0][0]["content"]
    assert seen[0][1]["content"].startswith("CODE:\n### FILE")
    assert "EXISTING_PAGE:\nold" in seen[0][1]["content"]
    assert v["promptId"].startswith("judge-code@")


def test_build_code_changes_a_forced_full_sync_emits_every_page_as_new_even_when_nothing_changed():
    tree = {"c1": {"src/a.go": "same", "cmd/m.go": "same"}, "c2": {"src/a.go": "same", "cmd/m.go": "same"}}
    policy = {"pages": [{"path": "api.md", "scope": ["src/**"]}, {"path": "cli.md", "scope": ["cmd/**"]}]}
    args = dict(repo="o/r", policy=policy, commit="c2", before="c1", **git_stub(tree))
    assert len(build_code_changes(**args)) == 0, "unchanged commit: nothing to do"
    full = build_code_changes(**args, full=True)
    assert [u["filePath"] for u in full] == ["api.md", "cli.md"]
    assert all(u["before"] is None for u in full), "before is empty so the prefilter sees a brand-new page"
