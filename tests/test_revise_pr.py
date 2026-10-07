"""Review feedback loop: multisync.cli.revise_pr with httpx.MockTransport for GitHub and the scripted fake LLM."""
import json

import httpx
import pytest

from multisync.cli import revise_pr as R
from multisync.factstore import MemoryFactStore
from multisync.review_outcomes import SUPERSEDED_MARKER, GitHub, collect_rows
from multisync.testing import HALLUCINATION_JUDGE, PASS_JUDGE, fake_llm, make_deps

BODY = "Source: `o/src` @ `abc1234`\n\n| File | Precision | Recall | Style | Quality |\n|---|---|---|---|---|\n| `api.md` | 1.0 | 0.9 | 0.9 | 0.9 |\n\nMerging indexes this text.\n"
TARGET = "site/docs/services/proj/api.md"
FIRST = "---\ntitle: Api\n---\n\nOLD PAGE TEXT with a human edit\n"
A_V2 = 'package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n'
POLICY = {"mode": "code", "trust": "review", "serviceName": "proj", "pages": [{"path": "api.md", "kind": "API documentation", "scope": ["src/**"], "brief": "The API."}]}


class Acc:
    def list_files(self, rev):
        return ["src/a.go"]

    def read_at(self, rev, f):
        return A_V2 if f == "src/a.go" else None

    def changed_between(self, a, b):
        return ["src/a.go"]


def fake_gh(*, comments=(), pr=None, review=None, inline=(), files=(TARGET,)):
    posted = []
    pr_ = {"state": "open", "body": BODY, "user": {"login": "docs-bot[bot]"}, "head": {"ref": "docs-sync/o-src-abc1234", "sha": "t1"}, "base": {"ref": "qa"}, **(pr or {})}
    rev = {"state": "CHANGES_REQUESTED", "body": "Please make the port clearer", "user": {"login": "ruben", "type": "User"}, "author_association": "OWNER", **(review or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p.endswith("/issues/9/comments"):
            posted.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={})
        if p.endswith("/issues/9/comments"):
            return httpx.Response(200, json=[{"body": c} for c in [*comments, *posted]])
        if p.endswith("/pulls/9"):
            return httpx.Response(200, json=pr_)
        if p.endswith("/pulls/9/files"):
            return httpx.Response(200, json=[{"filename": f} for f in files])
        if p.endswith("/reviews/555"):
            return httpx.Response(200, json=rev)
        if p.endswith("/reviews/555/comments"):
            return httpx.Response(200, json=list(inline))
        return httpx.Response(404)

    return GitHub("o/docs", "tok", httpx.MockTransport(handler)), posted


def stored_facts(tier="cheap"):
    f = MemoryFactStore()
    f.record_decision({"runId": "r", "repo": "o/src", "path": "api.md", "commit": "abc1234def0", "outcome": "pending_review", "attempts": [],
                       "metrics": {"diffClassification": "internal", "modelTier": tier, "policyVersion": "v1-aaaa", "final": {"precision": 1.0, "recall": 0.9, "style": 0.9, "quality": 0.9}}})
    return f


def setup(tmp_path, llm=None, tier="cheap"):
    (tmp_path / TARGET).parent.mkdir(parents=True)
    (tmp_path / TARGET).write_text(FIRST)
    llm = llm or fake_llm([PASS_JUDGE])
    facts = stored_facts(tier)
    return llm, facts, make_deps(llm=llm, facts=facts, policy=POLICY)


def revise(gh, deps, tmp_path):
    return R.run_revision(gh, number=9, review_id=555, base_branch="qa", deps=deps, acc=Acc(), root=str(tmp_path), commit="abc1234def0123456789abcdef0123456789abcd")


def test_happy_path_redrafts_from_the_current_page_with_the_review_as_feedback_on_the_stored_tier(tmp_path):
    llm, facts, deps = setup(tmp_path)
    gh, posted = fake_gh(inline=[{"path": TARGET, "line": 4, "body": "name the env var"}, {"path": "src/content/docs/other.md", "line": 1, "body": "unrelated"}])
    res = revise(gh, deps, tmp_path)
    assert res["status"] == "revised" and res["changed"] == [TARGET] and res["pages"][0]["status"] == "revised"
    assert res["pages"][0]["scores"]["quality"] == 0.9
    new = (tmp_path / TARGET).read_text()
    assert new != FIRST and new.startswith("---\ntitle:")
    first_draft = llm.calls["drafts"][0]
    assert "Please make the port clearer" in first_draft and "name the env var" in first_draft
    assert "OLD PAGE TEXT with a human edit" in first_draft, "the base is the current branch tip, human edits included"
    assert next(x for x in llm.calls["log"] if x["kind"] == "draft")["tier"] == "cheap", "the tier stored with the original decision is kept"
    assert any("other.md" in x for x in res["not_acted"]), "a comment on a file that is not a page is reported, not acted on"
    assert posted == [], "the success comment is posted only after the push (--post)"
    text = R.render_comment(res, "1234567890")
    assert text.startswith("<!-- multisync:revised review=555 -->") and "`api.md`" in text and "Not changed:" in text and "other.md" in text


def test_expensive_tier_is_kept_too(tmp_path):
    llm, facts, deps = setup(tmp_path, tier="expensive")
    revise(fake_gh()[0], deps, tmp_path)
    assert next(x for x in llm.calls["log"] if x["kind"] == "draft")["tier"] == "expensive"


def test_gate_failure_writes_nothing_and_says_which_checks_failed(tmp_path):
    llm, facts, deps = setup(tmp_path, llm=fake_llm([HALLUCINATION_JUDGE]))
    gh, _ = fake_gh()
    res = revise(gh, deps, tmp_path)
    assert res["status"] == "failed" and res["changed"] == [] and (tmp_path / TARGET).read_text() == FIRST
    assert res["pages"][0]["status"] == "failed" and res["pages"][0]["failed_checks"]
    text = R.render_comment(res)
    assert text.startswith("<!-- multisync:revise-failed review=555 -->") and "nothing was pushed" in text and res["pages"][0]["failed_checks"][0] in text


def test_an_already_handled_review_is_a_no_op_before_any_model_call(tmp_path):
    for kind in ("revised", "revise-failed", "revise-capped"):
        llm, facts, deps = setup(tmp_path / kind)
        gh, posted = fake_gh(comments=[f"<!-- multisync:{kind} review=555 -->\nold"])
        res = revise(gh, deps, tmp_path / kind)
        assert res["status"] == "skipped" and "already handled" in res["reason"] and llm.calls["chat"] == 0 and posted == []


def test_round_cap_stops_after_three_rounds_and_asks_for_a_human(tmp_path):
    llm, facts, deps = setup(tmp_path)
    old = [f"<!-- multisync:revised review={i} -->" for i in (1, 2)] + ["<!-- multisync:revise-failed review=3 -->"]
    gh, posted = fake_gh(comments=old)
    res = revise(gh, deps, tmp_path)
    assert res["status"] == "skipped" and "round limit" in res["reason"] and llm.calls["chat"] == 0
    assert len(posted) == 1 and "<!-- multisync:revise-capped review=555 -->" in posted[0] and "edit the pages by hand" in posted[0]
    assert R.guard(gh, 9, 555, "qa")["proceed"] is False and len(posted) == 1, "the cap comment is itself the idempotency marker"
    ok, _ = fake_gh(comments=old[:2])
    assert R.guard(ok, 9, 555, "qa")["proceed"] is True


@pytest.mark.parametrize("label,kw", [
    ("superseded body", {"pr": {"body": BODY + SUPERSEDED_MARKER}}),
    ("superseded comment", {"comments": [SUPERSEDED_MARKER]}),
    ("closed", {"pr": {"state": "closed"}}),
    ("merged", {"pr": {"state": "open", "merged": True}}),
    ("human author", {"pr": {"user": {"login": "ruben"}}}),
    ("other branch", {"pr": {"head": {"ref": "feature/x"}}}),
    ("other base", {"pr": {"base": {"ref": "main"}}}),
    ("not changes requested", {"review": {"state": "APPROVED"}}),
    ("bot reviewer", {"review": {"user": {"login": "docs-bot[bot]", "type": "Bot"}}}),
    ("outsider", {"review": {"author_association": "NONE"}}),
])
def test_guards_never_act(tmp_path, label, kw):
    llm, facts, deps = setup(tmp_path)
    gh, posted = fake_gh(**kw)
    res = revise(gh, deps, tmp_path)
    assert res["status"] == "skipped", label
    assert llm.calls["chat"] == 0 and posted == [] and (tmp_path / TARGET).read_text() == FIRST


def test_only_pages_of_the_pr_are_touched_and_a_page_missing_from_the_pr_is_reported(tmp_path):
    llm, facts, deps = setup(tmp_path)
    gh, _ = fake_gh(files=["README.md"])
    res = revise(gh, deps, tmp_path)
    assert res["changed"] == [] and any("`api.md`" in x for x in res["not_acted"]) and llm.calls["chat"] == 0
    assert (tmp_path / TARGET).read_text() == FIRST


def test_empty_review_text_is_a_noop_with_a_comment(tmp_path):
    llm, facts, deps = setup(tmp_path)
    res = revise(fake_gh(review={"body": ""})[0], deps, tmp_path)
    assert res["status"] == "noop" and llm.calls["chat"] == 0
    assert R.render_comment(res).startswith("<!-- multisync:revise-failed review=555 -->")


def test_a_revision_commit_makes_the_merged_page_differ_so_the_outcome_is_draft_with_edition(tmp_path):
    """No row is written by the revision itself; at merge, collect_rows compares the FIRST commit with the tip and sees the bot's revision."""
    llm, facts, deps = setup(tmp_path)
    revise(fake_gh()[0], deps, tmp_path)
    revised = (tmp_path / TARGET).read_text()
    assert getattr(facts, "review_outcomes", []) == [], "revising records nothing in review_outcomes"
    import base64

    contents = {("c1"): FIRST, ("c9"): revised}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/pulls/9"):
            return httpx.Response(200, json={"body": BODY, "head": {"sha": "c9"}})
        if p.endswith("/pulls/9/files"):
            return httpx.Response(200, json=[{"filename": TARGET}])
        if p.endswith("/pulls/9/commits"):
            return httpx.Response(200, json=[{"sha": "c1"}])
        if "/contents/" in p:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(contents[request.url.params["ref"]].encode()).decode()})
        return httpx.Response(404)

    rows, skipped = collect_rows(facts, GitHub("o/docs", "t", httpx.MockTransport(handler)), 9, "https://x/pull/9", "ruben", "2026-10-05T12:00:00Z", merged=True)
    assert skipped == [] and [r["outcome"] for r in rows] == ["draft_with_edition"]
    assert rows[0]["model_tier_used"] == "cheap" and getattr(facts, "review_outcomes", []) == []


def test_feedback_collection_caps_and_clips():
    rev = {"body": "x" * 5000, "user": {"login": "r"}}
    fb, skipped = R.collect_feedback(rev, [{"path": "a.md", "line": 1, "body": "c"}] * 40, {"p.md": "a.md"})
    assert len(fb["p.md"]) == 1 + R.MAX_COMMENTS and len(fb["p.md"][0]) < 2100 and any("further inline" in s for s in skipped)
