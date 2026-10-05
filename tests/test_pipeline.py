import re

import pytest

from multisync.chunker import chunk_markdown
from multisync.pipeline import process_change
from multisync.prefilter import prefilter
from multisync.structure import diff_line_count, structural_change
from multisync.testing import DOC_V1, DOC_V2, HALLUCINATION_JUDGE, PASS_JUDGE, fake_llm, make_deps
from multisync.vectorstore import index_approved


def change(**over):
    return {"repo": "org/svc", "filePath": "docs/api.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2, **over}


# ── Layer 1: zero-LLM checks ─────────────────────────────────────────────────
def test_trivial_diff_is_skipped_before_any_network_call():
    deps = make_deps()
    d = process_change(change(after=DOC_V1 + "x\n"), deps)
    assert d["outcome"] == "skipped"
    assert d["rootCauseTag"] == "trivial_diff"
    assert deps["llm"].calls["chat"] + deps["llm"].calls["embed"] + deps["llm"].calls["judge"] == 0


def test_wording_only_change_has_no_structural_change_and_costs_no_tokens():
    before = "## Setup\n\nRun the installer carefully.\nThen reboot the machine now.\nFinally verify it works.\n"
    after = "## Setup\n\nExecute the installer with care.\nAfterwards restart the machine now.\nLastly confirm that it works.\n"
    p = prefilter(before, after, 3)
    assert p["proceed"] is False
    assert p["tag"] == "no_structural_change"


def test_adding_a_list_item_is_a_forced_shape_change():
    s = structural_change("- a\n- b\n", "- a\n- b\n- c\n")
    assert s["forced"]
    assert "list_items" in s["reasons"]


def test_array_literal_growth_is_detected():
    s = structural_change('const ch = ["a","b"];', 'const ch = ["a","b","c"];')
    assert "array_literals" in s["reasons"]


def test_diff_line_count_is_order_insensitive():
    assert diff_line_count("a\nb\nc", "c\nb\na")["total"] == 0


# ── Layer 3: similarity short-circuit ────────────────────────────────────────
def test_near_duplicate_with_unchanged_shape_only_rekeys_the_commit_hash():
    deps = make_deps(env={"MIN_DIFF_LINES": "2"})
    index_approved(deps["vectors"], deps["llm"], "org/svc", "docs/api.md", DOC_V2, "old0000")
    chat_before = deps["llm"].calls["chat"]
    d = process_change(change(before=DOC_V2, after=DOC_V2.replace("8080", "8081")), deps)
    assert d["outcome"] == "refreshed"
    assert deps["llm"].calls["chat"] == chat_before, "no generation tokens spent"
    assert deps["llm"].calls["judge"] == 0
    assert all(p["payload"]["commit"] == "abc1234def" for p in deps["vectors"].points.values())


def test_shape_change_overrides_high_similarity_and_forces_generation():
    deps = make_deps()
    index_approved(deps["vectors"], deps["llm"], "org/svc", "docs/api.md", DOC_V2, "old0000")
    d = process_change(change(before=DOC_V2, after=DOC_V2 + "- webhook\n- sms\n- teams\n"), deps)
    assert d["outcome"] != "refreshed"


# ── Layer 4/5: generation, trust level ───────────────────────────────────────
def test_review_trust_yields_pending_review_with_a_starlight_page_and_is_not_indexed_yet():
    deps = make_deps()
    d = process_change(change(), deps)
    assert d["outcome"] == "pending_review"
    assert d["reviewerAction"] == "needs_review"
    assert re.search(r"src/content/docs/services/svc/features/api\.md$", d["targetPath"])
    assert d["content"].startswith("---\ntitle: ")
    assert "commit: abc1234def" in d["content"]
    assert deps["vectors"].count() == 0, "drafts must never be indexed before approval"


def test_auto_trust_repo_yields_published():
    d = process_change(change(), make_deps(policy={"trust": "auto"}))
    assert d["outcome"] == "published"
    assert d["reviewerAction"] == "auto_published"


def test_hallucinated_claim_never_converges_fallback_with_attempt_history():
    d = process_change(change(), make_deps(llm=fake_llm([HALLUCINATION_JUDGE])))
    assert d["outcome"] == "fallback"
    assert d["reviewerAction"] == "auto_rejected"
    assert d["rootCauseTag"] == "iteration_cap_exceeded"
    assert d["action"] == "none"
    assert len(d["attempts"]) >= 2
    assert any(a.get("widened") for a in d["attempts"]), "one automatic retry with widened top_k"
    assert any("supports gRPC" in f for f in d["feedback"]), "unsupported claim is flagged for the human"


def test_critic_feedback_is_fed_into_the_next_draft_attempt():
    llm = fake_llm([HALLUCINATION_JUDGE, PASS_JUDGE])
    d = process_change(change(), make_deps(llm=llm))
    assert d["outcome"] == "pending_review"
    assert len(d["attempts"]) == 2
    assert "supports gRPC" in llm.calls["drafts"][1]


def test_grounded_but_awkward_draft_gets_a_polish_only_pass_and_is_rescored():
    llm = fake_llm([{**PASS_JUDGE, "quality": 0.4}, PASS_JUDGE])
    d = process_change(change(), make_deps(llm=llm))
    assert d["outcome"] == "pending_review"
    assert len(d["attempts"]) == 1, "polish pass resolves it without another full attempt"
    assert llm.calls["judge"] == 2


# ── Layer 2: cross-repo gate ─────────────────────────────────────────────────
REGISTRY = {"alert_channels_enum": {"owner": "org/svc", "requires": ["org/frontend"]}}


def test_registered_contract_point_with_unshipped_dependent_is_blocked_before_any_llm_spend():
    deps = make_deps(registry=REGISTRY)
    d = process_change(change(after=DOC_V2 + "\nUses `alert_channels_enum`.\n"), deps)
    assert d["outcome"] == "fallback"
    assert d["rootCauseTag"] == "cross_repo_incomplete"
    assert deps["llm"].calls["chat"] == 0


def test_cross_repo_gate_passes_once_the_dependent_repo_has_approved_docs():
    deps = make_deps(registry=REGISTRY)
    deps["facts"].save_claims("org/frontend", "docs/ui.md", "f1", [{"text": "UI renders alert_channels_enum", "supported": True}])
    deps["facts"].approve_claims("org/frontend", "docs/ui.md", "f1")
    d = process_change(change(after=DOC_V2 + "\nUses `alert_channels_enum`.\n"), deps)
    assert d["outcome"] == "pending_review"


# ── Deletions, indexing, chunking ────────────────────────────────────────────
def test_deleted_source_follows_trust_level_without_ai():
    deps = make_deps()
    d = process_change(change(after=None), deps)
    assert d["action"] == "delete"
    assert d["outcome"] == "pending_review"
    assert deps["llm"].calls["chat"] == 0


def test_index_approved_replaces_a_files_old_chunks_and_keys_them_by_commit():
    deps = make_deps()
    index_approved(deps["vectors"], deps["llm"], "org/svc", "a.md", DOC_V1, "c1")
    n = index_approved(deps["vectors"], deps["llm"], "org/svc", "a.md", DOC_V2, "c2")
    assert deps["vectors"].count("org/svc") == n
    assert all(p["payload"]["commit"] == "c2" for p in deps["vectors"].points.values())


def test_chunker_splits_on_headings_and_strips_front_matter():
    chunks = chunk_markdown("---\ntitle: x\n---\n## A\n\none\n\n## B\n\ntwo\n")
    assert [c["heading"] for c in chunks] == ["A", "B"]


def test_prefilter_a_changed_exported_signature_is_not_skipped_as_no_structural_change():
    before = "### FILE: a.ts\nexport function createPoll(title) {\n  return 1;\n}\n"
    p = prefilter(before, before.replace("createPoll(title)", "createPoll(title, options)"), 1)
    assert p["proceed"] is True
    assert p["forced"] is True
    assert re.search(r"public interface changed: createPoll", p["reason"])
    # while a pure internal change still is
    assert prefilter(before, before.replace("return 1", "return 1; // fine"), 1)["proceed"] is False
