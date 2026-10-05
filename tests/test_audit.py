import json
import random
from unittest import mock

import httpx
import pytest

from multisync.cli.audit import run as audit_run
from multisync.cli.metrics import build_parser, run_audit_gap
from multisync.factstore import MemoryFactStore
from multisync.review_outcomes import audit_gap, audit_sample_rate, record
from multisync.tickets import create_tickets


def row(i, outcome="draft_with_noedition", reviewer="alice", cls="internal", tier="cheap", pr="https://x/pull/", policy="v1-a"):
    return {"change_unit_id": f"o/src@abc1234def:p{i}.md", "repo": "o/src", "diff_classification": cls, "model_tier_used": tier, "outcome": outcome, "reviewed_by": reviewer,
            "reviewed_at": "2026-10-05T12:00:00Z", "policy_version": policy, "pr_url": f"{pr}{i}"}


def run_record(rows, rate, rng, facts=None, on_sampled=None):
    facts = facts or MemoryFactStore()
    with mock.patch("multisync.review_outcomes.collect_rows", return_value=(rows, [])):
        return facts, record(facts, None, 1, "https://x/pull/1", "alice", None, True, audit_rate=rate, rng=rng, on_sampled=on_sampled)


# ── sampling ─────────────────────────────────────────────────────────────────
def test_noedition_rows_are_sampled_at_roughly_the_configured_rate():
    rows = [row(i) for i in range(2000)]
    facts, res = run_record(rows, 0.10, random.Random(7).random)
    share = len(res["audit_sampled"]) / 2000
    assert 0.08 < share < 0.12, share
    assert sum(1 for x in facts.review_outcomes if x["audit_sampled"]) == len(res["audit_sampled"])
    assert res["audit_rate"] == 0.10


def test_only_unedited_drafts_are_sampled_and_the_extremes_are_exact():
    mixed = [row(1), row(2, "draft_with_edition"), row(3, "draft_rejected"), row(4)]
    _, all_in = run_record(mixed, 1.0, lambda: 0.999)
    assert len(all_in["audit_sampled"]) == 2, "edited and rejected drafts are never sampled"
    _, none = run_record([row(i) for i in range(50)], 0.0, lambda: 0.0)
    assert none["audit_sampled"] == []


def test_a_redelivered_event_does_not_sample_again_and_a_failing_ticket_does_not_undo_the_sampling():
    facts = MemoryFactStore()
    run_record([row(1)], 1.0, lambda: 0.0, facts)
    _, again = run_record([row(1)], 1.0, lambda: 0.0, facts)
    assert again["inserted"] == 0 and again["audit_sampled"] == [] and len(facts.review_outcomes) == 1

    def broken(audit_id, r):
        raise RuntimeError("tickets down")

    facts2, res = run_record([row(2)], 1.0, lambda: 0.0, None, on_sampled=broken)
    assert len(res["audit_sampled"]) == 1 and any("ticket not opened" in x for x in res["skipped"]) and facts2.review_outcomes[0]["audit_sampled"] is True


def test_the_sample_rate_comes_from_the_environment_with_safe_defaults():
    assert audit_sample_rate({}) == 0.10
    assert audit_sample_rate({"AUDIT_SAMPLE_RATE": "0.25"}) == 0.25
    assert audit_sample_rate({"AUDIT_SAMPLE_RATE": "7"}) == 1.0 and audit_sample_rate({"AUDIT_SAMPLE_RATE": "-1"}) == 0.0
    assert audit_sample_rate({"AUDIT_SAMPLE_RATE": "lots"}) == 0.10


# ── the queue and the second reviewer ────────────────────────────────────────
def sampled_store():
    facts = MemoryFactStore()
    run_record([row(1, reviewer="Alice")], 1.0, lambda: 0.0, facts)
    return facts


def test_the_queue_hides_the_first_reviewers_name_outcome_and_scores():
    facts = sampled_store()
    facts.review_outcomes[0].update(judge_score_precision=0.97, similarity_score=0.3)
    item = facts.pending_audits()[0]
    assert set(item) == {"id", "repo", "change_unit_id"}
    code, listing = audit_run(["list"], facts)
    shown = audit_run(["show", str(item["id"])], facts)[1]
    for text in (listing, shown):
        for secret in ("alice", "Alice", "noedition", "0.97", "pull/"):
            assert secret not in text, secret
    assert "github.com/o/src/tree/abc1234def" in shown and "p1.md" in shown


def test_the_audit_must_be_done_by_someone_other_than_the_original_reviewer():
    facts = sampled_store()
    i = facts.pending_audits()[0]["id"]
    code, text = audit_run(["submit", str(i), "--reviewer", "alice", "--accurate", "yes"], facts)
    assert code == 1 and "someone other than the original reviewer" in text and facts.audit_item(i)["audit_verified_accurate"] is None, "case-insensitive refusal"
    code, text = audit_run(["submit", str(i), "--reviewer", "bob", "--accurate", "no", "--notes", "route is wrong"], facts)
    assert code == 0 and facts.review_outcomes[0]["audit_reviewer"] == "bob" and facts.review_outcomes[0]["audit_verified_accurate"] is False
    assert audit_run(["submit", str(i), "--reviewer", "carol", "--accurate", "yes"], facts)[0] == 1, "an audit is final"
    assert facts.pending_audits() == []


def test_an_unsampled_row_cannot_be_audited():
    facts = MemoryFactStore()
    run_record([row(1)], 0.0, lambda: 0.0, facts)
    assert audit_run(["submit", "1", "--reviewer", "bob", "--accurate", "yes"], facts)[0] == 1


def test_the_audit_ticket_does_not_anchor_the_auditor():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": 77})

    t = create_tickets({"deskBaseUrl": "https://t.example", "deskToken": "k", "deskProjectId": "10", "deskTransport": "rest"}, httpx.MockTransport(handler))
    url = t.open_audit(5, "o/src", "abc1234def", "api.md")
    assert url == "https://t.example/tasks/77"
    created = next(b for m, p, b in sent if m == "PUT" and p.endswith("/projects/10/tasks"))
    body = created["description"] + created["title"]
    assert created["title"].startswith("[docs-audit] #5 o/src@abc1234")
    for hidden in ("alice", "noedition", "pull/", "unchanged", "without edit", "approved", "merged"):
        assert hidden not in body.lower(), hidden
    assert "github.com/o/src/tree/abc1234def" in body and "multisync audit submit 5" in body and "must not be the person who reviewed" in body


# ── audit gap ────────────────────────────────────────────────────────────────
def build(raw_noedition, raw_edition, accurate, inaccurate, pending=0):
    """A segment with the given reviewed outcomes, and audits spread over its unedited rows."""
    facts = MemoryFactStore()
    for i in range(raw_noedition):
        facts.record_review_outcome(row(i, reviewer="alice"))
    for i in range(raw_edition):
        facts.record_review_outcome(row(1000 + i, "draft_with_edition"))
    audited = [x for x in facts.review_outcomes if x["outcome"] == "draft_with_noedition"][: accurate + inaccurate + pending]
    for j, x in enumerate(audited):
        x["audit_sampled"] = True
        if j < accurate + inaccurate:
            x.update(audit_reviewer="bob", audit_verified_accurate=j < accurate)
    return facts


def gap(facts, *argv):
    return run_audit_gap(build_parser().parse_args(["audit-gap", "--segment", "internal:cheap", *argv]), facts)


def test_the_gap_math_compares_the_audited_accuracy_with_the_raw_no_edit_rate():
    res = audit_gap({"n": 100, "noedition": 80}, {"sampled": 10, "audited": 10, "accurate": 6})
    assert res["raw_noedition_pct"] == 80.0 and res["pct_confirmed_accurate"] == 60.0 and res["gap_pp"] == 20.0 and res["drift_warning"] is True
    ok = audit_gap({"n": 100, "noedition": 80}, {"sampled": 10, "audited": 10, "accurate": 9})
    assert ok["gap_pp"] == -10.0 and ok["drift_warning"] is False
    edge = audit_gap({"n": 100, "noedition": 80}, {"sampled": 10, "audited": 10, "accurate": 7})
    assert edge["gap_pp"] == 10.0 and edge["drift_warning"] is False, "exactly the limit is not 'more than' the limit"
    assert audit_gap({"n": 100, "noedition": 80}, {"sampled": 10, "audited": 10, "accurate": 7}, max_gap_pp=5)["drift_warning"] is True


def test_cli_warns_when_the_audited_accuracy_is_more_than_ten_points_below_the_raw_rate():
    code, text = gap(build(40, 10, accurate=6, inaccurate=4))          # raw 80%, audited 60%
    assert code == 1 and "WARNING" in text and "rubber-stamped" in text
    assert "raw no-edit rate        80.0%" in text and "confirmed accurate      60.0%" in text and "+20.0 percentage points" in text
    code, text = gap(build(40, 10, accurate=9, inaccurate=1))          # audited 90%
    assert code == 0 and "WARNING" not in text and "no drift" in text


def test_pending_audits_are_reported_but_are_not_counted_as_inaccurate():
    code, text = gap(build(40, 10, accurate=9, inaccurate=1, pending=5))
    assert code == 0 and "15 sampled, 10 with a verdict, 5 pending" in text and "90.0%" in text


def test_too_few_verdicts_give_no_drift_warning_and_say_so():
    code, text = gap(build(40, 10, accurate=0, inaccurate=2))          # 0% of 2: noise, not a signal
    assert code == 0 and "WARNING" not in text and "2 of the 5 audits needed" in text
    assert gap(build(40, 10, accurate=0, inaccurate=2), "--min-audits", "2")[0] == 1, "the minimum is configurable"
    code, text = gap(build(10, 0, accurate=0, inaccurate=0))
    assert code == 0 and "no audit has a verdict yet" in text


def test_the_threshold_and_json_output_are_configurable():
    facts = build(40, 10, accurate=7, inaccurate=3)                    # gap exactly 10
    assert gap(facts)[0] == 0 and gap(facts, "--max-gap", "5")[0] == 1
    code, text = gap(facts, "--max-gap", "5", "--json")
    out = json.loads(text)
    assert code == 1 and out["drift_warning"] is True and out["segment"] == "internal:cheap" and out["gap_pp"] == pytest.approx(10.0) and out["audited"] == 10


def test_segments_and_policy_versions_are_kept_apart():
    facts = build(40, 10, accurate=6, inaccurate=4)
    other = run_audit_gap(build_parser().parse_args(["audit-gap", "--segment", "public_interface:expensive"]), facts)
    assert "0 of 0 reviewed" in other[1] or "0 sampled" in other[1]
    assert gap(facts, "--policy-version", "v1-zzz")[1].count("0 sampled") == 1
