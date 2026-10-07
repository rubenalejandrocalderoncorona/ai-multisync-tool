import json
import re
from pathlib import Path
from unittest import mock

import httpx

from multisync import segment_alerts
from multisync.cli.audit import run as audit_run
from multisync.factstore import MemoryFactStore
from multisync.review_outcomes import record
from multisync.segment_alerts import Notifier, check_drift, check_graduation, run_after_record, sampled_handler, secure_random
from multisync.tickets import create_tickets

_n = {"i": 0}


def mkrow(outcome="draft_with_noedition", cls="internal", tier="cheap", policy="v1-a", pr=None, reviewer="alice", at=None):
    _n["i"] += 1
    i = _n["i"]
    return {"change_unit_id": f"o/src@abc1234def:p{i}.md", "repo": "o/src", "diff_classification": cls, "model_tier_used": tier, "outcome": outcome, "reviewed_by": reviewer,
            "reviewed_at": at or f"2026-10-{(i % 27) + 1:02d}T12:00:00Z", "policy_version": policy, "pr_url": pr or f"https://github.com/o/docs/pull/{i}"}


def seed(facts, n, edited=0, **kw):
    rows = [mkrow(**kw) for _ in range(n)] + [mkrow("draft_with_edition", **kw) for _ in range(edited)]
    for r in rows:
        facts.record_review_outcome(r)
    return rows


class Slack:
    """A Slack webhook that records what it receives (or fails)."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def transport(self):
        def handler(req):
            if self.fail:
                return httpx.Response(500)
            self.sent.append(json.loads(req.content)["text"])
            return httpx.Response(200)
        return httpx.MockTransport(handler)

    def notifier(self, tickets=None):
        return Notifier({"slackWebhook": "https://hooks.slack.test/x"}, tickets, self.transport())


class FakeTickets:
    enabled = True

    def __init__(self, fail=False):
        self.infos, self.fail = [], fail

    def open_info(self, title, body):
        if self.fail:
            raise RuntimeError("desk down")
        self.infos.append((title, body))
        return "https://desk/tasks/1"


# ── graduation ───────────────────────────────────────────────────────────────
def test_graduation_fires_exactly_once_with_the_template_and_the_audit_trail():
    facts, slack, tk = MemoryFactStore(), Slack(), FakeTickets()
    rows = seed(facts, 40)
    n = slack.notifier(tk)
    fired = check_graduation(facts, rows, n, env={})
    assert len(fired) == 1 and fired[0]["slack"] is True and fired[0]["ticket"] == "https://desk/tasks/1"
    text = slack.sent[0]
    for part in ("🎓 Segment Graduation Alert!", "Segment: `internal:cheap` (Policy: `v1-a`)", "Sample Count: 40 / 30 min", "Unedited Rate: 100.0% (40/40)",
                 "Wilson 95% Lower Bound: 0.912 (Threshold: 0.85)", "A human may now consider enabling auto-approval for it (nothing is enabled by this alert)."):
        assert part in text, part
    title, body = tk.infos[0]
    assert "INFORMATIONAL" in body and "[docs-info]" in title
    for part in ("Reviewed drafts: 40", "Wilson interval", "lower bound &gt;= 0.85 and n &gt;= 30", "Policy version:</strong> <code>v1-a", "First reviewed: 2026-10-", "Last reviewed: 2026-10-",
                 "Reviewers: alice", rows[0]["pr_url"], rows[-1]["pr_url"]):
        assert part in body, part
    # a redelivery or the next PR of the same segment and policy does not announce again (the marker)
    assert check_graduation(facts, rows, n, env={}) == [] and len(slack.sent) == 1 and len(tk.infos) == 1


def test_graduation_needs_enough_samples_and_a_high_enough_lower_bound():
    slack = Slack()
    few = MemoryFactStore()
    assert check_graduation(few, seed(few, 29), slack.notifier(), env={}) == []
    weak = MemoryFactStore()
    assert check_graduation(weak, seed(weak, 80, edited=20), slack.notifier(), env={}) == []
    assert slack.sent == []
    ok = MemoryFactStore()
    assert check_graduation(ok, seed(ok, 20), slack.notifier(), env={"REVIEW_READINESS_MIN_SAMPLES": "20", "REVIEW_READINESS_THRESHOLD": "0.8"})  # env knobs


def test_a_different_policy_version_is_a_new_segment_and_alerts_again():
    facts, slack = MemoryFactStore(), Slack()
    n = slack.notifier()
    assert check_graduation(facts, seed(facts, 40, policy="v1-a"), n, env={})
    assert check_graduation(facts, seed(facts, 40, policy="v2-b"), n, env={})
    assert len(slack.sent) == 2


def test_demo_and_synthetic_data_never_fire_unless_the_env_allows_it():
    slack = Slack()
    demo = MemoryFactStore()
    assert check_graduation(demo, seed(demo, 40, policy="demo-v1"), slack.notifier(), env={}) == []
    synth = MemoryFactStore()
    assert check_graduation(synth, seed(synth, 40, pr="synthetic://seed/1"), slack.notifier(), env={}) == []
    assert slack.sent == [] and demo.segment_alerts == {} if hasattr(demo, "segment_alerts") else True
    allowed = MemoryFactStore()
    assert len(check_graduation(allowed, seed(allowed, 40, policy="demo-v1"), slack.notifier(), env={"GRADUATION_ALERT_SYNTHETIC": "1"})) == 1


def test_a_failing_slack_or_ticket_is_logged_never_raised_and_keeps_the_marker():
    facts = MemoryFactStore()
    rows = seed(facts, 40)
    n = Slack(fail=True).notifier(FakeTickets(fail=True))
    fired = check_graduation(facts, rows, n, env={})
    assert len(fired) == 1 and fired[0]["slack"] is False and fired[0]["ticket"] is None
    assert not facts.claim_segment_alert("graduation", "internal", "cheap", "v1-a"), "the marker survived the failures"


def test_a_crashing_store_never_raises():
    class Broken(MemoryFactStore):
        def review_outcome_counts(self, *a, **k):
            raise RuntimeError("db gone")
    facts = Broken()
    assert check_graduation(facts, [mkrow()], Notifier(None), env={}) == []
    assert run_after_record(facts, [mkrow()], Notifier(None), env={}) == {"graduation": [], "drift": []}


def test_open_info_creates_a_lowest_priority_informational_ticket():
    seen = {}

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": 9})
    t = create_tickets({"deskToken": "t", "deskProjectId": "10", "deskBaseUrl": "https://desk", "deskTransport": "rest"}, httpx.MockTransport(handler))
    assert t.open_info("[docs-info] x", "<p>body</p>") == "https://desk/tasks/9"
    assert seen["body"]["title"] == "[docs-info] x" and seen["body"]["priority"] == 1
    assert create_tickets({}).open_info("t", "b") is None


# ── audit sampling ───────────────────────────────────────────────────────────
def run_record(rows, rate, rng, on_sampled=None):
    facts = MemoryFactStore()
    with mock.patch("multisync.review_outcomes.collect_rows", return_value=(rows, [])):
        return facts, record(facts, None, 1, "https://x/pull/1", "alice", None, True, audit_rate=rate, rng=rng, on_sampled=on_sampled)


def test_sampling_uses_the_injected_rng_and_only_flags_unchanged_merges():
    draws = []

    def rng():
        draws.append(1)
        return 0.0
    rows = [mkrow(), mkrow("draft_with_edition"), mkrow("draft_rejected"), mkrow()]
    facts, res = run_record(rows, 0.10, rng)
    assert len(draws) == 2, "edited and rejected drafts are not even drawn for"
    assert [x["audit_sampled"] for x in facts.review_outcomes] == [True, False, False, True]
    assert len(res["audit_sampled"]) == 2


def test_the_default_draw_is_the_system_csprng():
    assert isinstance(segment_alerts._SYSTEM_RNG, __import__("secrets").SystemRandom)
    assert all(0.0 <= secure_random() < 1.0 for _ in range(100))
    rows = [mkrow()]
    with mock.patch("multisync.segment_alerts.secure_random", return_value=0.5) as m:
        facts, res = run_record(rows, 0.6, None)
    assert m.called and res["audit_sampled"], "with no rng injected, record draws from segment_alerts.secure_random"


class FakeGh:
    def __init__(self, fail=False):
        self.comments, self.fail = [], fail

    def comment(self, number, body):
        if self.fail:
            raise RuntimeError("403")
        self.comments.append((number, body))


def test_the_audit_pr_comment_is_posted_once_per_pr_with_the_configured_rate_and_never_fails():
    gh, slack = FakeGh(), Slack()
    _, res = run_record([mkrow(), mkrow(), mkrow()], 1.0, lambda: 0.0, on_sampled=sampled_handler(gh, 7, 0.10, None, slack.notifier()))
    assert len(res["audit_sampled"]) == 3
    assert gh.comments == [(7, "🔍 [Audit Spot-Check] 10% Quality Sample. A second reviewer must confirm this page against the code.")]
    assert len(slack.sent) == 1 and "Audit Spot-Check" in slack.sent[0]
    # no webhook configured: no Slack; comment failure does not raise or lose the sampling
    gh2 = FakeGh(fail=True)
    _, res2 = run_record([mkrow()], 1.0, lambda: 0.0, on_sampled=sampled_handler(gh2, 8, 0.25, None, Notifier(None)))
    assert len(res2["audit_sampled"]) == 1 and "25%" in segment_alerts.audit_comment_text(0.25)
    # an unsampled merge posts nothing
    gh3 = FakeGh()
    run_record([mkrow()], 0.0, lambda: 0.5, on_sampled=sampled_handler(gh3, 9, 0.0))
    assert gh3.comments == []


# ── drift ────────────────────────────────────────────────────────────────────
def audited(facts, accurate_flags, **kw):
    for ok in accurate_flags:
        r = mkrow(**kw)
        facts.record_review_outcome(r)
        aid = facts.sample_for_audit(r["pr_url"], r["change_unit_id"])
        facts.submit_audit(aid, "bob", ok)


def test_drift_fires_once_below_the_accuracy_alert_with_enough_audits():
    facts, slack = MemoryFactStore(), Slack()
    n = slack.notifier()
    audited(facts, [True, True, True, False])
    assert check_drift(facts, "internal", "cheap", "v1-a", n, env={}) is None, "4 audits is below the 5 needed"
    audited(facts, [False])  # 3/5 = 60%
    d = check_drift(facts, "internal", "cheap", "v1-a", n, env={})
    assert d and d["kind"] == "drift" and d["slack"] is True
    text = slack.sent[0]
    assert "🚨 Drift Alert" in text and "`internal:cheap`" in text and "60.0% (3/5" in text and "suspend any auto-approval for this segment until a human re-validates" in text
    assert check_drift(facts, "internal", "cheap", "v1-a", n, env={}) is None and len(slack.sent) == 1, "the marker keeps it to one alert"


def test_no_drift_alert_at_or_above_the_limit_or_for_another_segment():
    facts, slack = MemoryFactStore(), Slack()
    audited(facts, [True] * 4 + [False])  # exactly 0.80 is not below 0.80
    assert check_drift(facts, "internal", "cheap", "v1-a", slack.notifier(), env={}) is None
    assert check_drift(facts, "internal", "cheap", "v1-a", slack.notifier(), env={"AUDIT_ACCURACY_ALERT": "0.9"}), "the threshold is configurable"
    assert check_drift(facts, "public_interface", "expensive", "v1-a", slack.notifier(), env={}) is None


def test_submitting_a_verdict_through_the_audit_cli_triggers_the_drift_check():
    facts, slack = MemoryFactStore(), Slack()
    rows = [mkrow() for _ in range(5)]
    for r in rows:
        facts.record_review_outcome(r)
        facts.sample_for_audit(r["pr_url"], r["change_unit_id"])
    for i in range(1, 5):
        audit_run(["submit", str(i), "--reviewer", "bob", "--accurate", "no"], facts, slack.notifier())
    assert slack.sent == [], "four verdicts: not enough audits yet"
    code, _ = audit_run(["submit", "5", "--reviewer", "bob", "--accurate", "no"], facts, slack.notifier())
    assert code == 0 and len(slack.sent) == 1 and "Drift Alert" in slack.sent[0]


# ── the standing rule ────────────────────────────────────────────────────────
def test_no_new_code_path_touches_the_auto_approval_flag():
    root = Path(__file__).resolve().parent.parent / "multisync"
    for rel in ("segment_alerts.py", "tickets.py", "cli/audit.py", "cli/record_review.py", "review_outcomes.py", "cli/metrics.py"):
        for no, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            assert "auto_approval_eligible" not in code, f"{rel}:{no} references the approval flag"
    # the only writer in the store is the pre-existing constant FALSE on insert; the new methods never mention it
    store = (root / "factstore.py").read_text(encoding="utf-8")
    for name in ("claim_segment_alert", "segment_trail"):
        body = re.search(rf"def {name}\(.*?(?=\n    def |\nclass )", store, re.S).group(0)
        assert "auto_approval_eligible" not in body
