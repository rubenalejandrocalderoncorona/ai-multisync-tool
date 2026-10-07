from datetime import datetime, timezone

import pytest

from multisync.cli import phoenix_review_evals as cli
from multisync.factstore import MemoryFactStore
from multisync.observability import phoenix_exporter as pe
from multisync.review_outcomes import readiness, wilson_interval

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def row(i, outcome="draft_with_noedition", cls="internal", tier="cheap", policy="v1-aaa", pr="https://github.com/o/r/pull/1", prec=0.9, **kw):
    return {"change_unit_id": f"o/r@abc:p{i}.md", "repo": "o/r", "diff_classification": cls, "model_tier_used": tier, "similarity_score": 0.8, "judge_score_precision": prec,
            "judge_score_recall": 0.9, "judge_score_style": 0.8, "judge_score_quality": 0.7, "symbol_coverage_pct": None, "outcome": outcome, "reviewed_by": "alice",
            "reviewed_at": datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc), "policy_version": policy, "pr_url": pr, "auto_approval_eligible": False, **kw}


class FakeSpans:
    def __init__(self):
        self.stored, self.calls = {}, []

    def log_spans(self, project_identifier, spans):
        self.calls.append((project_identifier, spans))
        dup = 0
        for s in spans:
            key = (s["context"]["trace_id"], s["context"]["span_id"])
            dup += key in self.stored
            self.stored.setdefault(key, s)
        return {"total_received": len(spans), "total_queued": len(spans) - dup, "total_duplicates": dup}


class FakeClient:
    def __init__(self):
        self.spans = FakeSpans()


class Boom:
    def __getattr__(self, name):
        raise AssertionError("network used")


def test_grouping_and_wilson_numbers():
    rows = [row(i) for i in range(8)] + [row(8, "draft_with_edition"), row(9, "draft_rejected")] + [row(i, cls="public_interface", tier="expensive") for i in range(10, 13)] + [row(20, policy="v1-bbb")]
    m = {(x["policy_version"], x["segment"]): x for x in pe.segment_metrics(rows, min_samples=10, threshold=0.5)}
    assert set(m) == {("v1-aaa", "internal:cheap"), ("v1-aaa", "public_interface:expensive"), ("v1-bbb", "internal:cheap")}
    g = m[("v1-aaa", "internal:cheap")]
    lo, hi = wilson_interval(8, 10)
    assert (g["n"], g["noedition"], g["edition"], g["rejected"]) == (10, 8, 1, 1)
    assert g["no_edition_rate"] == 0.8 and g["wilson_lower_bound"] == lo and g["wilson_upper_bound"] == hi
    assert g["graduation_progress"] == 1.0 and g["graduation_n"] == 10
    assert g["ready"] == readiness({"n": 10, "noedition": 8}, 0.5, 10)["ready"]
    small = m[("v1-aaa", "public_interface:expensive")]
    assert small["graduation_progress"] == pytest.approx(0.3) and small["ready"] is False
    assert pe.segment_metrics([row(i) for i in range(50)], min_samples=30, threshold=0.85)[0]["graduation_progress"] == 1.0
    assert pe.segment_metrics([row(i) for i in range(50)], min_samples=30, threshold=0.85)[0]["graduation_n"] == 50


def test_ready_rule_matches_readiness():
    rows = [row(i) for i in range(40)]
    g = pe.segment_metrics(rows, min_samples=30, threshold=0.85)[0]
    r = readiness({"n": 40, "noedition": 40}, 0.85, 30)
    assert g["ready"] is r["ready"] is True and g["wilson_lower_bound"] == r["wilson_lower"]


def test_correlation_hand_computed():
    # scores 1,2,3,4,5 vs outcomes 0,0,1,1,1: cov=3.0, sxx=10, syy=1.2 -> 3.0/sqrt(12)=0.866025
    xs, ys = [1, 2, 3, 4, 5], [0, 0, 1, 1, 1]
    assert pe.pearson(xs, ys) == pytest.approx(0.8660254038, abs=1e-9)
    rows = [row(i, "draft_with_noedition" if y else "draft_with_edition", prec=x) for i, (x, y) in enumerate(zip(xs, ys))]
    rows.append(row(99, "draft_rejected", prec=0.0))  # rejected rows are excluded
    assert pe.segment_metrics(rows, min_samples=5, threshold=0.5)[0]["judge_vs_human_correlation"]["precision"] == pytest.approx(0.8660254038)


def test_correlation_none_cases():
    assert pe.pearson([1, 2, 3, 4], [0, 1, 0, 1]) is None  # n < 5
    assert pe.pearson([0.5] * 6, [0, 1, 0, 1, 0, 1]) is None  # score has zero variance
    assert pe.pearson([1, 2, 3, 4, 5], [1] * 5) is None  # outcome has zero variance
    g = pe.segment_metrics([row(i, prec=0.9) for i in range(6)], min_samples=5, threshold=0.5)[0]
    assert g["judge_vs_human_correlation"]["precision"] is None


def test_spans_and_session_and_time():
    rows = [row(1), row(2, reviewed_at=None, recorded_at=datetime(2026, 10, 2, tzinfo=timezone.utc), cls="public_interface", tier="expensive")]
    c = FakeClient()
    notes = []
    res = pe.sync_review_evals(rows, c, min_samples=5, threshold=0.5, now=NOW, annotate=lambda cl, n: notes.extend(n))
    (project, spans), (_, seg) = c.spans.calls
    assert project == "multirepo-agent-docs" and res["review_spans"] == 2 and res["segment_spans"] == 2
    s = spans[0]
    a = s["attributes"]
    assert s["name"] == "review_outcome" and a["session.id"] == "internal:cheap" and a["multisync.change_unit_id"] == "o/r@abc:p1.md"
    assert a["multisync.pr_url"].endswith("/pull/1") and a["multisync.outcome"] == "draft_with_noedition" and a["multisync.reviewer"] == "alice"
    assert a["multisync.policy_version"] == "v1-aaa" and a["multisync.synthetic"] is False and a["multisync.judge.precision"] == 0.9
    assert s["start_time"].startswith("2026-10-01T09:00") and spans[1]["start_time"].startswith("2026-10-02")
    assert seg[0]["name"] == "review_segment_metrics" and seg[0]["attributes"]["multisync.n"] == 1 and "multisync.wilson_lower_bound" in seg[0]["attributes"]
    assert len(notes) == 2 and notes[0]["span_id"] == seg[0]["context"]["span_id"]


def test_idempotent_ids():
    rows = [row(i) for i in range(3)]
    c = FakeClient()
    first = pe.sync_review_evals(rows, c, min_samples=5, threshold=0.5, now=NOW, annotate=lambda *a: 0)
    second = pe.sync_review_evals(rows, c, min_samples=5, threshold=0.5, now=NOW, annotate=lambda *a: 0)
    assert first["review_spans"] == 3 and second["review_spans"] == 0 and second["duplicates"] == 4
    assert len(c.spans.stored) == 4
    assert pe.review_ids("u", "c") == pe.review_ids("u", "c") != pe.review_ids("u", "d")
    assert pe.segment_ids("s", "v", "2026-10-07") != pe.segment_ids("s", "v", "2026-10-08")
    tid, sid = pe.review_ids("u", "c")
    assert len(tid) == 32 and len(sid) == 16
    # same segment, same policy, next UTC day: a new segment span
    later = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
    pe.sync_review_evals(rows, c, min_samples=5, threshold=0.5, now=later, annotate=lambda *a: 0)
    assert len(c.spans.stored) == 5


def test_synthetic_tagged():
    rows = [row(1, pr="synthetic://seed/1"), row(2)]
    c = FakeClient()
    res = pe.sync_review_evals(rows, c, min_samples=5, threshold=0.5, now=NOW, annotate=lambda *a: 0)
    tags = [s["attributes"]["multisync.synthetic"] for s in c.spans.calls[0][1]]
    assert tags == [True, False] and res["synthetic_rows"] == 1
    assert c.spans.calls[1][1][0]["attributes"]["multisync.synthetic_n"] == 1


def test_dry_run_no_network_and_cli_table(capsys):
    res = pe.sync_review_evals([row(1)], Boom(), min_samples=5, threshold=0.5, dry_run=True)
    assert res["review_spans"] == 0 and res["segments"][0]["n"] == 1
    facts = MemoryFactStore()
    facts.review_outcomes = [row(i) for i in range(3)]
    assert cli.main(["--dry-run", "--min-samples", "5"], env={}, facts=facts, client=Boom()) == 0
    out = capsys.readouterr().out
    assert "internal:cheap" in out and "Informational only" in out and "exported" not in out


def test_readonly_on_factstore_and_sql():
    class Q:
        def __init__(self):
            self.sql = []

        def _q(self, sql, params=None):
            self.sql.append(sql)
            return []

    q = Q()
    pe.sync_review_evals(q, None, min_samples=5, threshold=0.5, dry_run=True)
    assert len(q.sql) == 1 and q.sql[0].lstrip().upper().startswith("SELECT") and "auto_approval_eligible" not in q.sql[0]


def test_a_rerun_where_phoenix_raises_on_duplicates_counts_them_instead_of_failing():
    class Dup(Exception):
        pass

    Dup.__name__ = "SpanCreationError"

    class Client:
        class spans:
            @staticmethod
            def log_spans(project_identifier, spans):
                raise Dup(f"Found {len(spans)} duplicate spans:\n  - Span x")

    assert pe._log_spans(Client, "p", [{"a": 1}, {"a": 2}]) == {"total_received": 2, "total_queued": 0, "total_duplicates": 2}


def test_a_span_creation_error_that_is_not_all_duplicates_still_fails():
    class Dup(Exception):
        pass

    Dup.__name__ = "SpanCreationError"

    class Client:
        class spans:
            @staticmethod
            def log_spans(project_identifier, spans):
                raise Dup("Found 1 duplicate spans:\n  - Span x")

    import pytest

    with pytest.raises(Dup):
        pe._log_spans(Client, "p", [{"a": 1}, {"a": 2}])


def test_annotating_waits_for_phoenix_to_ingest_the_spans():
    class R:
        status_code = 404

    class NotFound(Exception):
        response = R()

    calls, sleeps = [], []

    def call():
        calls.append(1)
        if len(calls) < 3:
            raise NotFound()
        return "ok"

    assert pe._retry_until_ingested(call, attempts=5, delay=0, sleep=sleeps.append) == "ok" and len(calls) == 3 and len(sleeps) == 2


def test_annotating_gives_up_after_the_attempts_and_never_retries_other_errors():
    class R:
        def __init__(self, c): self.status_code = c

    class Err(Exception):
        def __init__(self, c): self.response = R(c)

    import pytest

    n = []
    with pytest.raises(Err):
        pe._retry_until_ingested(lambda: (n.append(1), (_ for _ in ()).throw(Err(404)))[1], attempts=3, delay=0, sleep=lambda s: None)
    assert len(n) == 3
    m = []
    with pytest.raises(Err):
        pe._retry_until_ingested(lambda: (m.append(1), (_ for _ in ()).throw(Err(500)))[1], attempts=3, delay=0, sleep=lambda s: None)
    assert len(m) == 1
