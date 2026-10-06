import base64
import json
import types

import httpx
import pytest

from multisync.cli.metrics import build_parser, parse_segment, run
from multisync.factstore import MemoryFactStore
from multisync.pipeline import process_change
from multisync.review_outcomes import GitHub, collect_rows, parse_pr_body, readiness, record, wilson_interval
from multisync.router import route_change
from multisync.testing import PASS_JUDGE, fake_llm, make_deps
from multisync.webhook import Config, job_manifest, plan

# ── Wilson score interval ────────────────────────────────────────────────────
@pytest.mark.parametrize("k,n,lo,hi", [(90, 100, 0.8256, 0.9448), (30, 30, 0.8864, 1.0), (9, 10, 0.5958, 0.9821), (0, 20, 0.0, 0.1611)])
def test_wilson_interval_matches_reference_values(k, n, lo, hi):
    got = wilson_interval(k, n)
    assert got[0] == pytest.approx(lo, abs=1e-3) and got[1] == pytest.approx(hi, abs=1e-3)


def test_wilson_with_no_samples_and_a_different_confidence():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert wilson_interval(90, 100, z=1.645)[0] > wilson_interval(90, 100, z=1.96)[0], "a lower confidence gives a tighter interval"


def test_readiness_needs_both_enough_samples_and_a_high_enough_lower_bound():
    ok = readiness({"n": 40, "noedition": 40, "edition": 0, "rejected": 0})
    assert ok["ready"] and ok["wilson_lower"] > 0.85 and ok["reasons"] == []
    few = readiness({"n": 20, "noedition": 20})
    assert not few["ready"] and any("only 20" in r for r in few["reasons"])
    weak = readiness({"n": 100, "noedition": 80, "edition": 15, "rejected": 5})
    assert not weak["ready"] and weak["rate"] == 0.8 and any("below the threshold" in r for r in weak["reasons"])
    assert readiness({"n": 100, "noedition": 80}, threshold=0.7, min_samples=50)["ready"], "threshold and minimum are configurable"
    assert readiness({"n": 0, "noedition": 0})["ready"] is False


_n = {"i": 0}


def seed(facts, cls, tier, noedition, edition=0, rejected=0, policy="v1-aaaa"):
    for outcome, count in (("draft_with_noedition", noedition), ("draft_with_edition", edition), ("draft_rejected", rejected)):
        for _ in range(count):
            _n["i"] += 1
            i = _n["i"]
            facts.record_review_outcome({"change_unit_id": f"o/r@c{i}:p.md", "repo": "o/r", "diff_classification": cls, "model_tier_used": tier, "outcome": outcome,
                                         "policy_version": policy, "pr_url": f"https://x/pull/{i}"})


def args(*argv):
    return build_parser().parse_args(["review-readiness", *argv])


def test_cli_reports_the_interval_and_exits_0_when_ready_1_when_not():
    facts = MemoryFactStore()
    seed(facts, "internal", "cheap", 40)
    seed(facts, "public_interface", "expensive", 20, edition=10, rejected=2)
    code, text = run(args("--segment", "internal:cheap"), facts)
    assert code == 0 and "READY" in text and "NOT READY" not in text and "reviewed drafts       40" in text and "Wilson interval       [0.912" in text
    code, text = run(args("--segment", "public_interface:expensive"), facts)
    assert code == 1 and "NOT READY" in text and "below the threshold 0.85" in text and "read-only: nothing is auto-approved" in text
    code, text = run(args("--segment", "internal:expensive", "--json"), facts)
    out = json.loads(text)
    assert code == 1 and out["n"] == 0 and out["segment"] == "internal:expensive"


def test_cli_threshold_minimum_and_policy_version_are_configurable():
    facts = MemoryFactStore()
    seed(facts, "internal", "cheap", 50, policy="v1-new")
    seed(facts, "internal", "cheap", 5, edition=45, policy="v1-old")
    assert run(args("--segment", "internal:cheap"), facts)[0] == 1, "mixed policy versions drag the rate down"
    assert run(args("--segment", "internal:cheap", "--policy-version", "v1-new"), facts)[0] == 0
    assert run(args("--segment", "internal:cheap", "--policy-version", "v1-new", "--threshold", "0.99"), facts)[0] == 1
    assert run(args("--segment", "internal:cheap", "--policy-version", "v1-new", "--min-samples", "60"), facts)[0] == 1


def test_segment_argument_is_validated():
    assert parse_segment("public_interface:expensive") == ("public_interface", "expensive")
    for bad in ("internal", "bogus:cheap", "internal:medium", ""):
        with pytest.raises(Exception):
            parse_segment(bad)


# ── collecting a closed PR ────────────────────────────────────────────────────
BODY = ("Source: `o/src` @ `abc1234`\n\n| File | Precision | Recall | Style | Quality |\n|---|---|---|---|---|\n| `overview.md` | 0.97 | 1.0 | 1.0 | 0.94 |\n"
        "| `api.md` | 1.0 | 0.9 | 0.9 | 0.9 |\n\nMerging indexes this text.\n\nTicket: x\n")
GEN = "---\ntitle: x\n---\n\n## A\n\ntext\n"


def decision(path, cls="public_interface", tier="expensive", cov=100.0):
    return {"runId": "r", "repo": "o/src", "path": path, "commit": "abc1234def0", "outcome": "pending_review", "metrics": {
        "minChunkSimilarity": 0.31, "final": {"precision": 0.97, "recall": 1.0, "style": 1.0, "quality": 0.94}, "diffClassification": cls, "modelTier": tier,
        "symbolCoverage": cov, "policyVersion": "v1-test"}, "attempts": []}


def fake_github(files, contents, first="c1", tip="c9", body=BODY):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"body": body, "head": {"sha": tip}})
        if path.endswith("/pulls/7/files"):
            return httpx.Response(200, json=[{"filename": f} for f in files])
        if path.endswith("/pulls/7/commits"):
            return httpx.Response(200, json=[{"sha": first}])
        if "/contents/" in path:
            ref = request.url.params["ref"]
            text = contents.get((path.split("/contents/")[1], ref))
            return httpx.Response(404) if text is None else httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        return httpx.Response(404)

    return GitHub("o/docs", "tok", httpx.MockTransport(handler)), calls


PATHS = ["src/content/docs/services/x/overview.md", "src/content/docs/services/x/api.md"]


def store_with_decisions():
    f = MemoryFactStore()
    for p in ("overview.md", "api.md"):
        f.record_decision(decision(p, "internal" if p == "api.md" else "public_interface", "cheap" if p == "api.md" else "expensive"))
    return f


def test_parse_pr_body_finds_the_source_commit_and_pages():
    assert parse_pr_body(BODY) == {"repo": "o/src", "sha": "abc1234", "pages": ["overview.md", "api.md"]}
    assert parse_pr_body("a human wrote this") is None


def test_merged_untouched_page_is_noedition_and_an_edited_one_is_edition_with_features_from_the_stored_decision():
    gh, _ = fake_github(PATHS, {(PATHS[0], "c1"): GEN, (PATHS[0], "c9"): GEN, (PATHS[1], "c1"): GEN, (PATHS[1], "c9"): GEN + "a reviewer's sentence\n"})
    rows, skipped = collect_rows(store_with_decisions(), gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", merged=True)
    assert skipped == [] and {r["change_unit_id"]: r["outcome"] for r in rows} == {
        "o/src@abc1234def0:overview.md": "draft_with_noedition", "o/src@abc1234def0:api.md": "draft_with_edition"}
    ov = next(r for r in rows if r["change_unit_id"].endswith("overview.md"))
    assert (ov["diff_classification"], ov["model_tier_used"], ov["similarity_score"], ov["symbol_coverage_pct"], ov["policy_version"]) == ("public_interface", "expensive", 0.31, 100.0, "v1-test")
    assert (ov["judge_score_precision"], ov["judge_score_recall"], ov["judge_score_style"], ov["judge_score_quality"]) == (0.97, 1.0, 1.0, 0.94)
    assert (ov["reviewed_by"], ov["reviewed_at"], ov["repo"]) == ("ruben", "2026-10-05T12:00:00Z", "o/src")
    assert next(r for r in rows if r["change_unit_id"].endswith("api.md"))["model_tier_used"] == "cheap"


def test_closed_without_merging_is_rejected_and_needs_no_file_reads():
    gh, calls = fake_github([], {})
    rows, _ = collect_rows(store_with_decisions(), gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", merged=False)
    assert {r["outcome"] for r in rows} == {"draft_rejected"} and len(rows) == 2
    assert not any("/contents/" in c or c.endswith("/files") for c in calls)


def test_one_row_per_draft_a_redelivered_event_adds_nothing_and_unknown_drafts_are_skipped_not_guessed():
    facts = store_with_decisions()
    gh, _ = fake_github(PATHS, {(PATHS[0], "c1"): GEN, (PATHS[0], "c9"): GEN, (PATHS[1], "c1"): GEN, (PATHS[1], "c9"): GEN})
    first = record(facts, gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", True)
    again = record(facts, gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", True)
    assert (first["inserted"], again["inserted"], again["duplicates"]) == (2, 0, 2) and len(facts.review_outcomes) == 2
    assert all(r["auto_approval_eligible"] is False for r in facts.review_outcomes)
    rows, skipped = collect_rows(MemoryFactStore(), gh, 7, "https://x/pull/7", "r", None, True)
    assert rows == [] and len(skipped) == 2 and "no pending_review decision" in skipped[0]
    gh2, _ = fake_github(PATHS, {}, body="not ours")
    assert collect_rows(facts, gh2, 7, "u", "r", None, True)[0] == []


# ── the receiver: the existing merge routing is unchanged, a rejection is now also logged ──
def cfg():
    c = Config({"ALLOWED_REPOS": "o/src", "CENTRAL_REPO": "me/docs", "TARGET_BRANCH": "qa"}, secret="x" * 20)
    c.namespace = "multirepo"
    return c


def pr_event(merged, **over):
    pr = {"number": 7, "html_url": "https://github.com/me/docs/pull/7", "merged": merged, "merge_commit_sha": "abcdef1" if merged else None, "merged_at": "2026-10-05T12:00:00Z" if merged else None,
          "closed_at": "2026-10-05T12:00:00Z", "head": {"ref": "docs-sync/o-src-abc1234", "sha": "9" * 40}, "base": {"ref": "qa", "sha": "1234567"}}
    return {"action": "closed", "repository": {"full_name": "me/docs"}, "sender": {"login": "ruben"}, "pull_request": {**pr, **over}}


def test_merged_pr_still_starts_the_index_job_now_carrying_the_review_facts():
    j, why = plan(cfg(), "pull_request", pr_event(True))
    assert j and j["mode"] == "index" and j["merge_sha"] == "abcdef1" and j["base_sha"] == "1234567", why
    assert j["pr"] == {"number": 7, "url": "https://github.com/me/docs/pull/7", "merged": True, "by": "ruben", "at": "2026-10-05T12:00:00Z"}
    env = {e["name"]: e["value"] for e in job_manifest(cfg(), j, now=1)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (env["PR_NUMBER"], env["PR_MERGED"], env["REVIEWED_BY"], env["REVIEWED_AT"], env["JOB_MODE"]) == ("7", "true", "ruben", "2026-10-05T12:00:00Z", "index")


def test_closed_unmerged_docs_sync_pr_starts_a_review_job_that_only_logs():
    j, why = plan(cfg(), "pull_request", pr_event(False))
    assert j and j["mode"] == "review" and j["pr"]["merged"] is False, why
    m = job_manifest(cfg(), j, now=1)
    assert m["metadata"]["name"].startswith("multisync-review-")
    assert plan(cfg(), "pull_request", pr_event(False, head={"ref": "feature/x", "sha": "9" * 40}))[0] is None, "other PRs are still ignored"
    assert plan(cfg(), "pull_request", pr_event(False, base={"ref": "main", "sha": "1234567"}))[0] is None
    assert plan(cfg(), "pull_request", {**pr_event(False), "action": "opened"})[0] is None


def test_hostile_review_fields_never_reach_the_job():
    j, _ = plan(cfg(), "pull_request", {**pr_event(True), "sender": {"login": "x; rm -rf /"}})
    assert j["pr"]["by"] == ""
    j, _ = plan(cfg(), "pull_request", pr_event(True, html_url="https://evil.example/pull/7"))
    assert j["pr"]["url"] == ""


# ── the run keeps the features the outcome rows need ──────────────────────────
def test_the_decision_carries_classification_tier_symbol_coverage_and_policy_version():
    llm = fake_llm([PASS_JUDGE])
    d = process_change({"kind": "code", "repo": "o/r", "filePath": "overview.md", "commit": "abc1234", "before": "### FILE: src/a.go\nfunc Alert() {}\n",
                        "after": '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n',
                        "existing": "# Old\n", "changedFiles": ["src/a.go"], "styleKey": "API documentation"}, make_deps(llm=llm))
    m = d["metrics"]
    assert m["diffClassification"] == "public_interface" and m["modelTier"] == "expensive" and m["symbolCoverage"] == 100.0
    assert m["policyVersion"].startswith("v1-") and len(m["policyVersion"]) == 11 and "minChunkSimilarity" in m and "precision" in m["final"]


def test_classification_does_not_depend_on_router_force():
    change = {"kind": "code", "repo": "o/r", "filePath": "a.md", "before": "### FILE: a.ts\nexport function f(a) {}\n", "after": "### FILE: a.ts\nexport function f(a, b) {}\n"}
    free = route_change(change)
    forced = route_change(change, force="cheap")
    assert free["classification"] == forced["classification"] == "public_interface" and forced["tier"] == "cheap" and free["tier"] == "expensive"
    internal = {"kind": "code", "repo": "o/r", "filePath": "a.md", "before": "### FILE: a.ts\nfunction f(a) { return 1 }\n", "after": "### FILE: a.ts\nfunction f(a) { return 2 }\n"}
    assert route_change(internal)["classification"] == route_change(internal, force="expensive")["classification"] == "internal"


def test_a_decision_stored_before_these_features_existed_is_skipped_never_given_guessed_labels():
    facts = MemoryFactStore()
    facts.record_decision({"runId": "old", "repo": "o/src", "path": "overview.md", "commit": "abc1234def0", "outcome": "pending_review",
                           "metrics": {"final": {"precision": 1.0}, "minChunkSimilarity": 0.2}, "attempts": []})  # no diffClassification, modelTier, policyVersion
    gh, _ = fake_github(PATHS, {})
    result = record(facts, gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", merged=False)
    assert result["rows"] == 0 and result["inserted"] == 0 and any("predates review-outcome features" in x for x in result["skipped"])
    assert getattr(facts, "review_outcomes", []) == []


# ── a newer sync supersedes older open drafts of the same repository ──────────
class FakeRepo:
    """The pull-request side of GitHub, enough for supersede_older."""

    def __init__(self, prs):
        self.prs = prs  # number -> {"ref": ..., "files": [...], "body": ...}
        self.closed, self.bodies, self.comments = [], {}, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, (json.loads(request.content) if request.content else {})
        if path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(200, json=[{"number": n, "head": {"ref": p["ref"]}, "body": p.get("body", "")} for n, p in self.prs.items() if n not in self.closed])
        if path.endswith("/files"):
            n = int(path.split("/")[-2])
            return httpx.Response(200, json=[{"filename": f} for f in self.prs[n]["files"]])
        if "/issues/" in path and path.endswith("/comments"):
            self.comments.append((int(path.split("/")[-2]), body["body"]))
            return httpx.Response(201, json={})
        if "/pulls/" in path and request.method == "PATCH":
            n = int(path.split("/")[-1])
            if "body" in body:
                self.bodies[n] = body["body"]
            if body.get("state") == "closed":
                self.closed.append(n)
            return httpx.Response(200, json={})
        return httpx.Response(404)


def supersede_world(prs):
    from multisync.review_outcomes import GitHub as GH
    fake = FakeRepo(prs)
    return fake, GH("o/docs", "tok", httpx.MockTransport(fake.handler))


def test_a_newer_draft_closes_older_ones_of_the_same_repo_whose_pages_it_covers_and_leaves_the_rest():
    from multisync.review_outcomes import SUPERSEDED_MARKER, supersede_older
    page = "src/content/docs/services/x/overview.md"
    other = "src/content/docs/services/x/api.md"
    fake, gh = supersede_world({
        24: {"ref": "docs-sync/o-src-1111111", "files": [page, other]},
        23: {"ref": "docs-sync/o-src-2222222", "files": [page]},                       # covered: closed
        22: {"ref": "docs-sync/o-src-3333333", "files": [page, other]},                # covered: closed
        21: {"ref": "docs-sync/o-src-4444444", "files": ["src/content/docs/services/x/data.md"]},  # a page the new one lacks: kept
        20: {"ref": "docs-sync/o-src-extra-5555555", "files": [page]},                 # another repository whose name starts the same: untouched
        19: {"ref": "feature/not-ours", "files": [page]},
    })
    res = supersede_older(gh, 24, "o/src", "qa")
    assert sorted(res["closed"]) == [22, 23] and [n for n, _ in res["kept"]] == [21]
    assert sorted(fake.closed) == [22, 23] and all(SUPERSEDED_MARKER in fake.bodies[n] for n in (22, 23))
    assert all("Superseded by #24" in c for _, c in fake.comments) and len(fake.comments) == 2


def test_a_superseded_pull_request_is_not_logged_as_a_rejected_review():
    gh, _ = fake_github([], {}, body=BODY + "\n<!-- multisync:superseded -->")
    rows, skipped = collect_rows(store_with_decisions(), gh, 7, "https://x/pull/7", "ruben", "2026-10-05T12:00:00Z", merged=False)
    assert rows == [] and "newer draft replaced it" in skipped[0]
