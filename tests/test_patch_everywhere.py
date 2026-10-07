"""Patch drafting beyond code mode: docs-mode pages (an edited source doc over an existing site page), revisions after a review, and the
visible draft-mode line in the PR body."""
import json
import re
import subprocess

from multisync.cli import revise_pr as R
from multisync.patching import doc_diff, split_sections, strip_generated
from multisync.pipeline import find_site_page, process_change
from multisync.review_outcomes import parse_pr_body
from multisync.testing import PASS_JUDGE, fake_llm, make_deps

SECTIONS = ["Overview", "Installation", "Configuration", "Endpoints", "Authentication", "Errors", "Pagination", "Versioning"]


def page_section(name, body=None):
    return f"## {name}\n\n{body or f'The {name.lower()} of the alert service works as the team described it, with no surprises for operators.'}\n\n"


PAGE = "".join(page_section(n) for n in SECTIONS).rstrip("\n") + "\n"
SRC_V1 = "# Alert service\n\n" + "".join(f"## {n} (source)\n\nThe {n.lower()} text in the source.\n\n" for n in SECTIONS) + "Listen port: 8080.\nRate: 60 per minute.\n"
SRC_V2 = SRC_V1.replace("Listen port: 8080.", "Listen port: 9090.").replace("Rate: 60 per minute.", "Rate: 120 per minute.")
CONFIG_ID = "Configuration"


def config_op(text="## Configuration\n\nThe service listens on port 9090 and allows 120 requests per minute."):
    return {"op": "replace", "section": CONFIG_ID, "text": text}


def docs_unit(**o):
    return {"kind": "docs", "repo": "o/r", "filePath": "docs/FLOW.md", "commit": "abc1234", "before": SRC_V1, "after": SRC_V2, "existing": PAGE, **o}


def note(d, node):
    return [t["note"] for t in d["trail"] if t["node"] == node]


def run_docs(llm, u=None, **kw):
    return process_change(docs_unit(**(u or {})), make_deps(llm=llm, **kw))


# ── docs mode ────────────────────────────────────────────────────────────────
def test_docs_mode_edited_source_doc_over_an_existing_page_patches_only_the_affected_section():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
    d = run_docs(llm)
    assert d["outcome"] == "pending_review", d.get("reason")
    assert llm.calls["patch"] == 1 and not llm.calls["drafts"], "no full conversion"
    body = d["content"].split("\n---\n", 1)[1]
    old, new = split_sections(PAGE), split_sections(strip_generated(body.lstrip("\n")))
    assert [o["heading"] for o, n in zip(old, new) if o["text"] != n["text"]] == ["Configuration"] and len(old) == len(new)
    w = note(d, "write_draft")[-1]
    assert w["patchMode"] and w["sectionsChanged"] == 1 and w["sectionsTotal"] == 8 and re.match(r"^patch-docs@[0-9a-f]{8}$", w["prompt"])
    assert note(d, "verify_draft")[-1]["retainedPct"] >= 85
    m = d["metrics"]
    assert m["patchMode"] is True and m["retainedPct"] >= 85 and m["draftMode"] == "patch (1 of 8 sections changed)"
    inp = llm.calls["patchInputs"][0]
    assert "-Listen port: 8080." in inp and "+Listen port: 9090." in inp and "SOURCE_DOCUMENT:" in inp and "=== SECTION Configuration ===" in inp
    assert "FACT_SHEET" not in inp


def test_docs_mode_new_document_is_a_full_draft_with_the_reason_recorded():
    for u in ({"before": None, "existing": ""}, {"before": None}, {"existing": ""}):
        llm = fake_llm([PASS_JUDGE], patches=[{"operations": []}])
        d = run_docs(llm, u=u)
        assert llm.calls["patch"] == 0 and len(llm.calls["drafts"]) == 1
        assert d["metrics"]["patchMode"] is False and d["metrics"]["draftMode"].startswith("full draft (")
    assert "new source document" in d["metrics"]["draftMode"] or "no existing page" in d["metrics"]["draftMode"]


def test_docs_mode_deleted_document_is_unchanged():
    llm = fake_llm([PASS_JUDGE])
    d = run_docs(llm, u={"after": None})
    assert d["action"] == "delete" and llm.calls["patch"] == 0 and not llm.calls["drafts"]


def test_docs_mode_malformed_json_twice_falls_back_to_the_full_draft():
    llm = fake_llm([PASS_JUDGE], patches=["nope", "still nope"])
    d = run_docs(llm)
    assert llm.calls["patch"] == 2 and len(llm.calls["drafts"]) == 1
    w = note(d, "write_draft")[0]
    assert w["patchFallback"] is True and w["prompt"].startswith("draft-docs@")
    m = d["metrics"]
    assert m["patchMode"] is False and m["patchFallback"] is True and m["draftMode"].startswith("full draft (patch failed")


def test_docs_mode_knobs_turn_it_off():
    for env in ({"PATCH_DOCS_MODE": "0"}, {"PATCH_DRAFTING": "0"}):
        llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
        d = run_docs(llm, env=env)
        assert llm.calls["patch"] == 0 and d["metrics"]["patchMode"] is False


def test_docs_mode_patched_page_keeps_its_location_and_skips_folder_classification(tmp_path):
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
    d = run_docs(llm, u={"existingPath": "site/docs/services/svc/guides/FLOW.md"})
    assert d["targetPath"] == "site/docs/services/svc/guides/FLOW.md"
    assert not any(x["kind"] == "folder" for x in llm.calls["log"])


def test_find_site_page_searches_the_classified_folder(tmp_path):
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "FLOW.md").write_text("x")
    assert find_site_page(str(tmp_path), "docs/FLOW.md") == str(tmp_path / "guides" / "FLOW.md")
    assert find_site_page(str(tmp_path), "docs/OTHER.md") is None
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "FLOW.md").write_text("y")
    assert find_site_page(str(tmp_path), "docs/FLOW.md") is None, "ambiguous"
    (tmp_path / "FLOW.md").write_text("z")
    assert find_site_page(str(tmp_path), "docs/FLOW.md") == str(tmp_path / "FLOW.md")


def test_doc_diff_is_a_unified_diff_of_the_source_document():
    d = doc_diff(SRC_V1, SRC_V2, "docs/FLOW.md")
    assert d.startswith("--- a/docs/FLOW.md") and "-Listen port: 8080." in d and "+Rate: 120 per minute." in d


# ── revisions ────────────────────────────────────────────────────────────────
def revision_unit(feedback, **o):
    return {"kind": "code", "repo": "o/r", "filePath": "api.md", "commit": "abc1234", "before": "", "after": "package main\nfunc Alert() {}\n", "existing": PAGE,
            "reviewFeedback": feedback, "revisionPatch": True, "changedFiles": ["src/a.go"], "styleKey": "API documentation", **o}


def revise_run(llm, feedback, env=None, **u):
    deps = {**make_deps(llm=llm, env=env), "revision": True, "forceTier": "cheap", "escalate": None}
    return process_change(revision_unit(feedback, **u), deps)


def test_revision_patches_the_page_with_the_reviewer_comments_as_the_change():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
    d = revise_run(llm, ["Review by ruben: say the port is 9090", "Comment on site/docs/api.md:12: mention the rate limit"])
    assert d["outcome"] == "pending_review", d.get("reason")
    assert llm.calls["patch"] == 1 and not llm.calls["drafts"]
    inp = llm.calls["patchInputs"][0]
    assert "REVIEWER_REQUEST" in inp and "say the port is 9090" in inp and "site/docs/api.md:12" in inp and "Source diff" not in inp
    assert inp.count("say the port is 9090") == 1, "the comments are not repeated as 'rejected attempt' feedback"
    m = d["metrics"]
    assert m["patchMode"] is True and m["sectionsChanged"] == 1 and m["retainedPct"] >= 85 and m["draftMode"] == "patch (1 of 8 sections changed)"


def test_revision_in_docs_mode_uses_the_docs_prompt():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
    d = process_change(docs_unit(before=None, reviewFeedback=["Comment: fix the port"], revisionPatch=True), {**make_deps(llm=llm), "revision": True, "escalate": None})
    assert d["outcome"] == "pending_review" and llm.calls["patch"] == 1
    assert "REVIEWER_REQUEST" in llm.calls["patchInputs"][0] and "SOURCE_DOCUMENT:" in llm.calls["patchInputs"][0]
    assert note(d, "write_draft")[-1]["prompt"].startswith("patch-docs@")


def test_whole_page_comment_may_touch_many_sections_the_drift_guard_is_off_for_revisions():
    ops = [{"op": "replace", "section": s["id"], "text": s["text"].rstrip("\n") + "\n\nReworded."} for s in split_sections(PAGE)[:7]]
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": ops}])
    d = revise_run(llm, ["Review by ruben: restructure and reword the whole page"])
    assert d["outcome"] == "pending_review" and llm.calls["patch"] == 1
    assert d["metrics"]["sectionsChanged"] == 7 and all(n["ok"] for n in note(d, "verify_draft"))
    # ... a stricter share can be configured (a revision has no source diff, so it counts as a small change)
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": ops}, {"operations": [config_op()]}])
    d = revise_run(llm, ["reword"], env={"REVISE_PATCH_MAX_SECTION_SHARE": "0.5"})
    vs = note(d, "verify_draft")
    assert vs[0]["ok"] is False and vs[0]["reasons"][0].startswith("patch_too_broad") and d["outcome"] == "pending_review"


def test_revision_patch_failure_falls_back_to_the_full_redraft_with_the_comments_as_feedback():
    llm = fake_llm([PASS_JUDGE], patches=["nope", "nope"])
    d = revise_run(llm, ["Review by ruben: rewrite the intro"])
    assert llm.calls["patch"] == 2 and len(llm.calls["drafts"]) == 1
    assert "rewrite the intro" in llm.calls["drafts"][0]
    m = d["metrics"]
    assert m["patchMode"] is False and m["patchFallback"] is True and m["draftMode"].startswith("full draft (patch failed")


def test_revision_unknown_section_id_falls_back():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [{"op": "replace", "section": "Nope", "text": "## Nope\n\nx"}]}])
    d = revise_run(llm, ["x"])
    assert len(llm.calls["drafts"]) == 1 and d["metrics"]["patchFallback"] is True


def test_revision_knob_turns_patching_off():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [config_op()]}])
    d = revise_run(llm, ["x"], env={"PATCH_REVISIONS": "0"})
    assert llm.calls["patch"] == 0 and len(llm.calls["drafts"]) == 1 and "PATCH_REVISIONS=0" in d["metrics"]["draftMode"]


def test_revise_pr_page_change_marks_the_unit_for_patching(tmp_path):
    (tmp_path / "t.md").write_text("---\ntitle: T\n---\n\n" + PAGE)

    class Acc:
        def read_at(self, rev, f):
            return "# Doc\n\nbody\n" if f == "docs/FLOW.md" else None

    unit = R.page_change(repo="o/r", commit="abc1234", page="FLOW.md", target="t.md", policy={"mode": "docs"}, acc=Acc(), root=str(tmp_path), feedback=["Comment: x"])
    assert unit["revisionPatch"] is True and unit["before"] is None and unit["existing"].startswith("## Overview") and unit["reviewFeedback"] == ["Comment: x"]


def test_revise_comment_names_the_draft_mode():
    res = {"status": "revised", "review_id": 5, "reviewer": "r", "pages": [{"page": "api.md", "status": "revised", "draftMode": "patch (1 of 8 sections changed)"}], "not_acted": []}
    assert "- `api.md`: patch (1 of 8 sections changed)" in R.render_comment(res)


# ── PR body ──────────────────────────────────────────────────────────────────
OLD_BODY = "Source: `o/src` @ `abc1234`\n\n| File | Precision | Recall | Style | Quality |\n|---|---|---|---|---|\n| `overview.md` | 0.97 | 1.0 | 1.0 | 0.94 |\n| `api.md` | 1.0 | 0.9 | 0.9 | 0.9 |\n\nMerging indexes this text.\n"
NEW_BODY = ("Source: `o/src` @ `abc1234`\n\n| File | Precision | Recall | Style | Quality | Draft mode |\n|---|---|---|---|---|---|\n"
            "| `overview.md` | 0.97 | 1.0 | 1.0 | 0.94 | full draft (new source document) |\n| `api.md` | 1.0 | 0.9 | 0.9 | 0.9 | patch (1 of 8 sections changed) |\n\nMerging indexes this text.\n")


def test_parse_pr_body_reads_old_and_new_bodies():
    expected = {"repo": "o/src", "sha": "abc1234", "pages": ["overview.md", "api.md"]}
    assert parse_pr_body(OLD_BODY) == expected and parse_pr_body(NEW_BODY) == expected


def test_the_job_entrypoint_builds_a_body_the_parser_reads_with_the_draft_mode_per_page(tmp_path):
    script = open("scripts/job-entrypoint.sh", encoding="utf-8").read()
    jq = re.search(r"^jq -r '(.*)' pipeline-results\.json > pr-body\.md$", script, re.M).group(1)
    results = {"repo": "o/src", "commit": "abc1234def", "results": [
        {"outcome": "pending_review", "path": "docs/FLOW.md", "metrics": {"final": {"precision": 1, "recall": 1, "style": 1, "quality": 1}, "draftMode": "patch (1 of 8 sections changed)"}},
        {"outcome": "pending_review", "path": "old.md", "metrics": {"final": {"precision": 1, "recall": 1, "style": 1, "quality": 1}}},
        {"outcome": "skipped", "path": "x.md", "metrics": {}}]}
    (tmp_path / "pipeline-results.json").write_text(json.dumps(results))
    out = subprocess.run(["jq", "-r", jq, "pipeline-results.json"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert "| Draft mode |" in out and "| patch (1 of 8 sections changed) |" in out and "| - |" in out
    assert parse_pr_body(out) == {"repo": "o/src", "sha": "abc1234", "pages": ["docs/FLOW.md", "old.md"]}
