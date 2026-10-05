import re

from multisync.context import backfill_coupling, sync_context
from multisync.factstore import MemoryFactStore
from multisync.router import route_change
from multisync.testing import fake_llm
from multisync.vectorstore import MemoryVectorStore
from multisync.verify import verify_draft


def code(before, after, **over):
    return {"kind": "code", "repo": "o/r", "filePath": "api.md", "before": before, "after": after, "changedFiles": ["a.ts"], **over}


A = '### FILE: a.ts\nexport function createPoll(title) {\n  const x = 1;\n  console.log("created");\n  return x;\n}\n'


def test_router_internals_only_is_cheap():
    after = A.replace("const x = 1", "const renamed = 1").replace("return x", "return renamed").replace('"created"', '"poll created ok"')
    r = route_change(code(A, after))
    assert r["tier"] == "cheap"
    assert "internals only" in r["reasons"][0]


def test_router_a_changed_exported_signature_is_expensive_naming_the_symbol():
    r = route_change(code(A, A.replace("createPoll(title)", "createPoll(title, options)")))
    assert r["tier"] == "expensive"
    assert re.search(r"public interface touched \(1 changed\): createPoll", r["reasons"][0])
    assert r["signals"]["publicChanged"] == ["createPoll"]


def test_router_new_route_removed_export_new_env_var_schema_field_are_expensive():
    cases = {"route": A + 'app.get("/polls", h);', "removed": "### FILE: a.ts\nconst x = 1;\n", "env": A + "const p = process.env.POLL_LIMIT;",
             "model": A + "### FILE: m.prisma\nmodel Poll {\n id String\n}\n"}
    for label, after in cases.items():
        assert route_change(code(A, after))["tier"] == "expensive", label


def test_router_a_page_written_from_scratch_over_public_symbols_is_expensive_over_nothing_public_cheap():
    assert route_change(code(None, A))["tier"] == "expensive"
    assert route_change(code(None, "### FILE: a.ts\nconst x = 1;\n"))["tier"] == "cheap"


def test_router_a_symbol_in_the_cross_repo_registry_is_expensive_even_when_nothing_public_changed():
    r = route_change(code(A, A + "// uses alert_channels_enum\n"), registry={"alert_channels_enum": {"owner": "o/r", "requires": ["o/fe"]}})
    assert r["tier"] == "expensive"
    assert "cross-repo contract point: alert_channels_enum" in " ".join(r["reasons"])


def test_router_documents_in_other_repos_that_mention_the_changed_symbol_are_expensive_own_docs_do_not_count():
    facts = MemoryFactStore()
    ch = code(A, A.replace("createPoll(title)", "createPoll(title, o)"))
    facts.replace_doc_refs("o/r", "README.md", ["createPoll"], "source_doc")  # its own doc
    assert route_change(ch, facts=facts)["signals"]["referencedBy"] == 0
    facts.replace_doc_refs("o/other", "docs/usage.md", ["createPoll"], "source_doc")
    facts.replace_doc_refs("site:o/docs", "src/content/docs/g.md", ["createPoll"], "site_doc")
    r = route_change(ch, facts=facts)
    assert r["signals"]["referencedBy"] == 2
    assert "referenced by 2 document(s) elsewhere: createPoll" in " ".join(r["reasons"])


def test_router_docs_mode_defaults_to_cheap_and_force_overrides_everything():
    assert route_change({"kind": "docs", "repo": "o/r", "filePath": "docs/a.md", "before": "a", "after": "b"})["tier"] == "cheap"
    assert route_change(code(A, A), force="expensive")["tier"] == "expensive"
    assert route_change(code(A, A.replace("createPoll(title)", "createPoll()")), force="cheap")["tier"] == "cheap"


# ── deterministic verification ───────────────────────────────────────────────
GOOD = ("## Overview\n\nThe createPoll function creates a poll with a title and returns its id for later use by callers.\n\n"
        "## Usage\n\nCall `createPoll` with a title string. It returns the new poll id as a string value.\n")


def ok(**over):
    args = {"draft": GOOD, "names": ["createPoll"], "file_path": "api.md", "title": "T", "description": "D", **over}
    return verify_draft(**args)


def test_verify_a_good_draft_passes_with_metrics():
    v = ok()
    assert v["reasons"] == []
    assert v["ok"] is True
    assert v["metrics"]["mentioned"] == "1/1"
    assert v["metrics"]["frontMatter"] == "ok"


def test_verify_missing_symbol_names_are_listed_so_the_next_draft_can_fix_exactly_that():
    v = ok(names=["createPoll", "deletePoll", "listPolls"])
    assert v["ok"] is False
    assert "Name each of them: deletePoll, listPolls" in v["reasons"][0]


def test_verify_own_front_matter_empty_truncated_and_headingless_drafts_are_all_caught():
    assert "own front matter" in ok(draft=f"---\ntitle: x\n---\n{GOOD}")["reasons"][0]
    assert "too short" in " ".join(ok(title=None, description=None, draft="## A\n\n```\ncode\n```\n\n" + "x" * 10)["reasons"])
    assert not ok(draft=GOOD)["reasons"]
    assert "code fence is never closed" in " ".join(ok(draft=GOOD + "\n```bash\nnpm run")["reasons"])
    assert "mid-sentence" in " ".join(ok(draft=GOOD + "\nAnd then the function continues to")["reasons"])
    assert "no section headings" in " ".join(ok(draft="Plain text with no headings at all. " * 10)["reasons"])


def test_verify_dropped_content_and_padding_are_measured_against_the_existing_page():
    assert "size of the existing page: content was dropped" in " ".join(ok(existing="x" * 2000)["reasons"])
    assert "size of the existing page: check for repetition" in " ".join(ok(existing="y" * 420, draft=GOOD * 12)["reasons"])


# ── FactStore coupling: populated by the context sync ────────────────────────
class TreeGit:
    def __init__(self, tree, changed=None):
        self.tree, self._changed = tree, changed or []

    def list_files(self, rev):
        return list(self.tree.get(rev, {}))

    def read_at(self, rev, f):
        return self.tree.get(rev, {}).get(f)

    def changed_between(self, a, b):
        return self._changed


def test_sync_context_records_public_symbols_and_which_documents_mention_them():
    facts = MemoryFactStore()
    stores = dict(code_store=MemoryVectorStore(), doc_store=MemoryVectorStore(), llm=fake_llm(), facts=facts)
    tree = {"c1": {"src/a.ts": "export function createPoll(t) {}\nfunction local() {}\n", "src/b.ts": "export const MAX = 3;\n",
                   "README.md": "# R\n\nCall createPoll with a title. MAX is the limit.\n"}}
    r = sync_context(repo="o/r", commit="c1", before="", git=TreeGit(tree), **stores)
    assert r["symbols"] == 2
    assert len(facts.known_symbol_names()) == 2
    assert r["docRefs"] == 1, "README mentions createPoll (MAX is shorter than 4 characters and is ignored as prose)"
    info = facts.symbol_info("createPoll")
    assert info["defined"] == [{"repo": "o/r", "path": "src/a.ts", "kind": "export"}]
    assert info["referencedBy"] == [{"doc_repo": "o/r", "doc_path": "README.md", "kind": "source_doc"}]
    # incremental: only the changed file's symbols are replaced
    tree2 = {"c1": tree["c1"], "c2": {**tree["c1"], "src/a.ts": "export function createPoll(t, o) {}\nexport function dropPoll() {}\n"}}
    sync_context(repo="o/r", commit="c2", before="c1", git=TreeGit(tree2, ["src/a.ts"]), **stores)
    assert sorted(facts.known_symbol_names()) == ["MAX", "createPoll", "dropPoll"]


def test_backfill_coupling_fills_symbols_and_doc_references_including_the_docs_site_with_no_embeddings():
    facts = MemoryFactStore()
    tree = {"c1": {"src/a.ts": "export function createPoll(t) {}\n", "docs/g.md": "# G\n\nUse createPoll.\n"}}
    site = {"src/content/docs/x.md": "The createPoll function is documented here."}
    r = backfill_coupling(repo="o/r", commit="c1", git=TreeGit(tree), facts=facts, site_files=site, site_repo="o/docs")
    assert [r["symbols"], r["docRefs"], r["siteRefs"]] == [1, 1, 1]
    refs = facts.doc_refs(["createPoll"], exclude_repo="o/r")
    assert [x["doc_repo"] for x in refs] == ["site:o/docs"], "the repo's own docs are excluded, the site counts as elsewhere"
