"""Judging a patched draft: the production failure (a two-constant diff over a 22-section, already approved API reference never passed the
whole-page judge) and the guards around it."""
from multisync.config import policy_version
from multisync.patching import drop_unrelated, scope_of, split_sections
from multisync.pipeline import process_change
from multisync.testing import PASS_JUDGE, fake_llm, make_deps
from tests.test_patching import JAVA_V1, JAVA_V2

GET = "Listings API > Endpoints > GET /api/v1/listings"
TOPICS = ["Authentication", "Pagination", "Errors", "Rate limits", "Versioning", "Filtering", "Sorting", "Webhooks", "Idempotency", "Caching",
          "Localization", "Deprecation", "Security", "Testing", "Sandbox", "Support", "Glossary", "Changelog", "Limits and quotas"]


def big_page():
    out = ["# Listings API\n\nThis page describes the listings REST API and how to call it from a client application.\n\n",
           "## Endpoints\n\nThe endpoints of the listings service.\n\n",
           "### GET /api/v1/listings\n\nReturns listings. The page size is 50 and the maximum rate is 60 requests per minute.\n\n"]
    out += [f"## {t}\n\nThis section explains {t.lower()} as the service behaved when this page was approved by a reviewer.\n\n" for t in TOPICS]
    return "".join(out).rstrip("\n") + "\n"


PAGE = big_page()
SECS = split_sections(PAGE)


def unit(**o):
    return {"kind": "code", "repo": "o/r", "filePath": "api-reference.md", "commit": "abc1234", "before": JAVA_V1, "after": JAVA_V2, "existing": PAGE,
            "changedFiles": ["src/ListingController.java"], "styleKey": "API documentation", **o}


def good_op():
    sec = next(s for s in SECS if s["id"] == GET)
    return {"op": "replace", "section": GET, "text": sec["text"].replace("size is 50", "size is 100").replace("is 60 requests", "is 120 requests").rstrip("\n")}


def style_op(topic="Versioning"):
    sec = next(s for s in SECS if s["id"] == f"Listings API > {topic}" or s["id"] == topic)
    return {"op": "replace", "section": sec["id"], "text": sec["text"].replace("explains", "describes").rstrip("\n")}


def whole_page_low_recall(extra=None):
    """A judge that behaves like the production one: asked about the whole page it misses facts outside the change; asked about the changed
    sections only it is satisfied."""
    low = {"claims": [{"text": "old wording", "supported": False}, {"text": "ok", "supported": True}] * 5,
           "facts": [{"text": f"fact {i}", "covered": i < 3, "core": i < 2} for i in range(10)], "style": 0.9, "quality": 0.9, "notes": []}

    def judge(messages):
        if "CHANGED_SECTIONS:" in messages[1]["content"]:
            return {**PASS_JUDGE, **(extra or {})}
        return low
    return judge


def run(llm, env=None, **u):
    return process_change(unit(**u), make_deps(llm=llm, env={"MAX_ITERATIONS": "3", **(env or {})}))


def note(d, node):
    return [t["note"] for t in d["trail"] if t["node"] == node]


def test_before_the_fix_the_page_falls_back_after_it_is_accepted_in_one_attempt():
    legacy = fake_llm([whole_page_low_recall()], patches=[{"operations": [good_op()]}])
    d = run(legacy, env={"PATCH_SCOPED_JUDGE": "0", "PATCH_CONVERGE_AFTER": "0"})
    assert d["outcome"] == "fallback" and d["rootCauseTag"] == "iteration_cap_exceeded" and legacy.calls["judge"] == 6
    llm = fake_llm([whole_page_low_recall()], patches=[{"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review", d.get("reason")
    assert llm.calls["judge"] == 1 and len(d["attempts"]) == 1
    j = note(d, "judge")[0]
    assert j["scopedJudge"] is True and j["changedSections"] == [GET] and j["failure"] is None
    sent = llm.calls["judgeInputs"][0]
    assert "CHANGED_SECTIONS:\n### GET /api/v1/listings" in sent and "page size is 100" in sent
    changed_part = sent.split("CHANGED_SECTIONS:")[1].split("UNCHANGED_PAGE")[0]
    assert "size is 50" not in changed_part and "## Versioning" not in changed_part
    assert "## Versioning" in sent.split("UNCHANGED_PAGE")[1]


def test_carried_over_unsupported_claims_are_recorded_and_do_not_fail_the_attempt():
    carried = [{"text": "Rate limits are per API key", "supported": False}, {"text": "fine", "supported": True}]
    llm = fake_llm([whole_page_low_recall({"carriedOver": carried})], patches=[{"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review"
    assert note(d, "judge")[0]["carriedOverUnsupported"] == ["Rate limits are per API key"]
    # an unsupported claim IN the changed section still fails (precision)
    bad = {**PASS_JUDGE, "claims": [{"text": "supports gRPC", "supported": False}, {"text": "ok", "supported": True}]}
    llm = fake_llm([lambda m: bad], patches=[{"operations": [good_op()]}])
    d = run(llm, env={"PATCH_CONVERGE_AFTER": "0"})
    assert d["outcome"] == "fallback" and note(d, "judge")[0]["failure"] == "hallucinated_claim"


def test_unrelated_style_only_replace_is_dropped_and_the_section_stays_byte_identical():
    llm = fake_llm([whole_page_low_recall()], patches=[{"operations": [good_op(), style_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review"
    v = note(d, "verify_draft")[0]
    assert [x["section"] for x in v["droppedOps"]] == ["Listings API > Versioning"] or [x["section"] for x in v["droppedOps"]] == ["Versioning"]
    assert v["droppedOps"][0]["tag"] == "patch_unrelated_edit" and v["sectionsChanged"] == 1
    body = d["content"]
    assert "This section explains versioning as the service behaved" in body and "describes versioning" not in body
    assert note(d, "judge")[0]["changedSections"] == [GET]
    assert d["metrics"]["sectionsChanged"] == 1 and d["metrics"]["draftMode"].startswith("patch (1 of ")


def test_drop_unrelated_is_conservative_never_inserts_never_new_facts_and_keeps_related_sections():
    diff = "--- a/A.java\n+++ b/A.java\n@@\n-    private static final int PAGE_SIZE = 50;\n+    private static final int PAGE_SIZE = 100;\n"
    sec = {s["id"]: s for s in SECS}
    versioning = next(i for i in sec if i.endswith("Versioning"))
    ops = [good_op(), style_op(),
           {"op": "insert_after", "section": versioning, "text": "## Extra\n\nStyle only text without any identifier."},
           {"op": "replace", "section": next(i for i in sec if i.endswith("Caching")), "text": sec[next(i for i in sec if i.endswith("Caching"))]["text"].rstrip("\n") + "\n\nEntries live for 300 seconds."},
           {"op": "delete", "section": next(i for i in sec if i.endswith("Glossary"))}]
    kept, dropped = drop_unrelated(SECS, ops, diff)
    assert [d["section"] for d in dropped] == [versioning]
    assert len(kept) == 4 and {o["op"] for o in kept} == {"replace", "insert_after", "delete"}
    # a section whose old text names the changed constant is related even when the rewrite is only wording
    related = {"op": "replace", "section": GET, "text": sec[GET]["text"].replace("Returns", "Lists").rstrip("\n")}
    assert drop_unrelated(SECS, [related], diff)[1] == []
    assert drop_unrelated(SECS, ops, "")[1] == [], "no diff, nothing to compare with: nothing is dropped"
    s = scope_of(SECS, [good_op()])
    assert s["changedIds"] == [GET] and "## Versioning" in s["unchangedText"] and "### GET" not in s["unchangedText"]


def test_judge_feedback_for_the_next_attempt_names_the_changed_sections_only():
    bad = {**PASS_JUDGE, "claims": [{"text": "supports gRPC", "supported": False}, {"text": "ok", "supported": True}]}
    llm = fake_llm([lambda m: bad, lambda m: PASS_JUDGE], patches=[{"operations": [good_op(), style_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review" and llm.calls["patch"] == 2
    second = llm.calls["patchInputs"][1]
    assert f"Only these sections may be changed: {GET}." in second and "Unsupported claim, remove or correct" in second
    assert "Versioning." not in second.split("Fix exactly these problems:")[1]


def test_no_improvement_after_the_third_attempt_stops_early_with_a_clear_reason():
    def judge(recall):
        facts = [{"text": f"f{i}", "covered": i < round(recall * 20), "core": False} for i in range(20)]
        return {**PASS_JUDGE, "facts": facts}
    llm = fake_llm([judge(0.5), judge(0.5), judge(0.52)], patches=[{"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "fallback" and d["rootCauseTag"] == "iteration_cap_exceeded"
    assert llm.calls["judge"] == 3 and llm.calls["patch"] == 3
    assert "did not converge: no improvement" in d["reason"] and "3 attempt(s) avoided" in d["reason"]
    n = note(d, "judge")[-1]["noImprovement"]
    assert n["attemptsAvoided"] == 3 and n["firstRecall"] == 0.5 and n["bestRecall"] == 0.5
    # a loop that is improving keeps going and can still pass; the knob can switch the check off
    llm = fake_llm([judge(0.4), judge(0.55), judge(0.7), PASS_JUDGE], patches=[{"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review" and llm.calls["judge"] == 4
    llm = fake_llm([judge(0.5)], patches=[{"operations": [good_op()]}])
    d = run(llm, env={"PATCH_CONVERGE_AFTER": "0"})
    assert d["outcome"] == "fallback" and llm.calls["judge"] == 6 and "no improvement" not in d["reason"]


def test_full_drafts_keep_whole_page_judging_and_todays_thresholds_and_budget():
    cfg = make_deps()["cfg"]["thresholds"]
    assert (cfg["precisionMin"], cfg["recallMin"], cfg["coreRecallMin"], cfg["styleMin"], cfg["judgeMin"]) == (0.9, 0.85, 1, 0.7, 0.75)
    page = PAGE.replace("# Listings API\n\n", "", 1)
    llm = fake_llm([whole_page_low_recall()], draft=page)
    d = run(llm, before=None)
    assert d["outcome"] == "fallback" and llm.calls["judge"] == 6 and llm.calls["patch"] == 0, "no early stop for a full draft"
    assert "did not converge in 6 attempt(s)" in d["reason"]
    sent = llm.calls["judgeInputs"][0]
    assert "CHANGED_SECTIONS" not in sent and "\nDRAFT:\n" in sent
    assert not any(n.get("scopedJudge") for n in note(d, "judge"))
    # a patch that fell back to a full rewrite is judged whole too
    llm = fake_llm([whole_page_low_recall()], draft=page, patches=["nope", "nope"])
    d = run(llm, env={"PATCH_CONVERGE_AFTER": "0"})
    assert llm.calls["judge"] == 6 and not any(n.get("scopedJudge") for n in note(d, "judge"))


def test_the_new_knobs_are_part_of_the_policy_version():
    a = make_deps()["cfg"]
    b = make_deps(env={"PATCH_MIN_IMPROVEMENT": "0.2"})["cfg"]
    assert a["thresholds"]["patchMinImprovement"] == 0.05 and a["thresholds"]["patchConvergeAfter"] == 3
    assert policy_version(a) != policy_version(b)
