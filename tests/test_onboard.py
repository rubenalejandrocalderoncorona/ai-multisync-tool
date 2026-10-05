import re

from multisync.cli.onboard_check import plan_onboarding
from multisync.prompts import load_styles

STYLES = load_styles()


class Tree:
    def __init__(self, files):
        self.files = files

    def list_files(self, rev):
        return list(self.files)

    def read_at(self, rev, f):
        return self.files.get(f)


def plan(files, policy):
    return plan_onboarding("o/r", policy, Tree(files), "abc1234", STYLES)


def test_a_sound_config_yields_a_plan_with_counts_an_embedding_estimate_and_no_blockers():
    files = {"src/a.go": "package main\nfunc A() {}\n", "src/b.go": "package main\nfunc B() {}\n", "src/a_test.go": "x", "README.md": "# R\n\ntext\n", "package-lock.json": "{}"}
    r = plan(files, {"mode": "code", "trust": "review", "glossary": {"X": "y"}, "pages": [{"path": "overview.md", "kind": "README / project overview", "brief": "b", "scope": ["src/**", "README.md"]}]})
    assert r["blockers"] == []
    assert r["code"]["files"] == 2, "tests and lockfiles are never indexed"
    assert r["semantic"]["files"] == 1 and r["semantic"]["chunks"] >= 2
    assert r["embeddings"]["tokens"] > 0 and r["embeddings"]["usd"] >= 0
    assert r["pages"][0]["scopeFiles"] == 3


def test_a_page_whose_scope_matches_nothing_is_a_blocker():
    r = plan({"src/a.go": "x"}, {"mode": "code", "pages": [{"path": "p.md", "scope": ["nope/**"], "brief": "b"}]})
    assert "scope matches NO files" in "\n".join(r["blockers"])


def test_restricted_content_and_secret_type_paths_in_scope_are_blockers_excluding_them_clears_it():
    files = {"src/a.go": "x", "notes/n.md": "---\nclassification: Oracle Restricted\n---\ntext", ".env.production": "A=1"}
    pol = {"mode": "code", "pages": [{"path": "p.md", "scope": ["**"], "brief": "b"}]}
    assert "look sensitive" in "\n".join(plan(files, pol)["blockers"])
    assert plan(files, {**pol, "exclude": ["notes/**", ".env*"]})["blockers"] == []


def test_secret_looking_lines_are_reported_warnings_cover_missing_brief_glossary_and_auto_trust():
    r = plan({"src/a.go": 'const API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\nfunc A() {}'}, {"mode": "code", "trust": "auto", "pages": [{"path": "p.md", "scope": ["src/**"]}]})
    w = "\n".join(r["warnings"])
    assert "look like secrets; those lines are removed" in w
    assert 'no "brief"' in w
    assert "no glossary" in w
    assert 'trust is "auto"' in w


def test_coverage_styles_report_how_many_declared_names_the_page_must_cover_unknown_styles_warn():
    files = {"prisma/m.prisma": "model A {\n id String\n}\nmodel B {\n id String\n}\n"}
    r = plan(files, {"mode": "code", "glossary": {"x": "y"}, "pages": [
        {"path": "d.md", "kind": "Data and schema reference", "brief": "b", "scope": ["prisma/**"]},
        {"path": "e.md", "kind": "No such style", "brief": "b", "scope": ["prisma/**"]}]})
    assert r["pages"][0]["coverage"]["prisma_model"] == 2
    assert 'style "No such style" is not in config/doc-styles.json' in "\n".join(r["warnings"])


def test_extra_docs_globs_count_as_semantic_context():
    r = plan({"src/a.go": "x", "apps/docs/g.mdx": "# G\n\ntext\n"}, {"mode": "code", "glossary": {"x": "y"}, "docs": ["apps/docs/**/*.mdx"], "pages": [{"path": "p.md", "scope": ["src/**"], "brief": "b"}]})
    assert r["semantic"]["files"] == 1


def test_ordinary_source_files_named_after_credentials_are_not_flagged_real_secret_files_and_classified_notes_are():
    pol = {"mode": "code", "glossary": {"x": "y"}, "pages": [{"path": "p.md", "scope": ["src/**"], "brief": "b"}]}
    ok = plan({"src/features/credentials/data.ts": "export const x = 1", "src/secrets-manager.ts": "x", "src/poll.ts": 'const visibility = "restricted"'}, pol)
    assert ok["blockers"] == []
    allpol = {"mode": "code", "glossary": {"x": "y"}, "pages": [{"path": "p.md", "scope": ["**"], "brief": "b"}]}
    for f in ("config/secrets.yaml", "deploy/secrets/db.txt", "keys/id_rsa"):
        bad = plan({"src/a.go": "x", f: "x"}, allpol)
        assert "look sensitive" in "\n".join(bad["blockers"]), f
    # .env / .pem / .key are excluded by default, so they never reach the model and are not blockers
    assert plan({"src/a.go": "x", ".env": "A=1", "certs/s.pem": "x"}, allpol)["blockers"] == []
