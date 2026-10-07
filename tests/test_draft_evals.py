import json

import httpx
import pytest

from multisync import tracing
from multisync.cli import evals as evals_cli
from multisync.config import load_config
from multisync.evals import draft_evals as de
from multisync.evals import run_draft_evals
from multisync.evals.judge import PipelineJudge
from multisync.factstore import MemoryFactStore
from multisync.llm import LLM
from multisync.pipeline import process_change
from multisync.testing import DOC_V1, DOC_V2, PASS_JUDGE, fake_llm, make_deps
from tests.test_tickets import REST_CFG, rest_desk
from multisync.tickets import create_tickets

pd = pytest.importorskip("pandas")
pytest.importorskip("phoenix.evals")
pytest.importorskip("phoenix.client")

DOCS = [{"document.content": "The sync job runs nightly at 02:00."}, {"document.content": "Retries are capped at five."}]


def span(sid, output="It runs nightly.", docs=DOCS, repo="o/r", commit="abc1234def", page="docs/a.md", attempt=1):
    return {"context.span_id": sid, "input.value": "## Fact sheet\n{}\n\n## Doc plan\n{}", "output.value": output, "retrieval.documents": docs,
            "multisync.change_unit_id": f"{repo}@{commit}:{page}", "multisync.repo": repo, "multisync.commit": commit, "multisync.page": page,
            "multisync.attempt": attempt, "multisync.tier": "cheap"}


def frame(*rows):
    return pd.DataFrame(list(rows)).set_index("context.span_id")


class FakeSpans:
    """What phoenix.client.Client().spans offers to the evaluator, in memory: rows to read, annotations that accumulate (upsert on name + span)."""

    def __init__(self, rows, scores=None):
        self.rows, self.logged, self.calls = rows, dict(scores or {}), []

    def get_spans_dataframe(self, **kw):
        self.calls.append(kw)
        return frame(*self.rows) if self.rows else pd.DataFrame()

    def log_span_annotations_dataframe(self, *, dataframe, annotation_name, annotator_kind, sync):
        assert (annotation_name, annotator_kind, sync) == (de.EVAL_NAME, "LLM", True)
        for r in dataframe.to_dict("records"):
            self.logged[r["span_id"]] = {"annotation_name": annotation_name, "result.score": r["score"], "result.label": r["label"], "result.explanation": r["explanation"]}
        return [{"id": i} for i in range(len(dataframe))]

    def get_span_annotations_dataframe(self, *, span_ids, **kw):
        got = [{"span_id": s, **self.logged[s]} for s in span_ids if s in self.logged]
        return pd.DataFrame(got).set_index("span_id") if got else pd.DataFrame()


class FakeClient:
    def __init__(self, rows, scores=None):
        self.spans = FakeSpans(rows, scores)


def judge_llm(verdicts):
    """A cheap-tier model that answers with the next verdict: 'faithful' or 'unfaithful'."""
    seen = iter(verdicts)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"label": next(seen), "explanation": "because"})}}], "usage": {}})

    return LLM(load_config({"INTERNAL_AI_API_KEY": "k", "DEEPSEEK_API_KEY": "d"})["ai"], httpx.MockTransport(handler))


# ── the draft span the pipeline emits ────────────────────────────────────────
@pytest.fixture()
def spans():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    assert tracing.configure(exporter=exp)
    yield exp
    tracing._tracer = tracing._provider = None


def test_the_pipeline_emits_a_draft_span_under_write_draft_with_the_attributes_the_evaluator_reads(spans):
    d = process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2}, make_deps(llm=fake_llm([PASS_JUDGE])))
    done = spans.get_finished_spans()
    drafts = [s for s in done if s.name == "draft"]
    assert len(drafts) == 1
    a = drafts[0].attributes
    assert a["multisync.change_unit_id"] == "o/r@abc1234def:docs/a.md" and a["multisync.repo"] == "o/r" and a["multisync.attempt"] == 1
    assert a["input.value"].startswith("## Fact sheet") and "## Doc plan" in a["input.value"]
    assert a["output.value"].strip() and d["outcome"] == "pending_review"
    parent = next(s for s in done if s.context.span_id == drafts[0].parent.span_id)
    assert parent.name == "node:write_draft"


def test_document_attrs_are_openinference_retrieval_documents_and_bounded():
    a = tracing.document_attrs([{"id": "c1", "content": "x" * 5000, "score": 0.5}, {"id": 2, "content": None}], limit=1, chars=100)
    assert set(a) == {"retrieval.documents.0.document.content", "retrieval.documents.0.document.id", "retrieval.documents.0.document.score"}
    assert len(a["retrieval.documents.0.document.content"]) < 200 and a["retrieval.documents.0.document.score"] == 0.5


# ── pure logic ───────────────────────────────────────────────────────────────
def test_build_inputs_maps_a_span_to_input_output_and_the_retrieved_chunks_as_reference():
    rows, skipped = de.build_inputs(frame(span("s1")).reset_index().to_dict("records"))
    assert skipped == [] and len(rows) == 1
    r = rows[0]
    assert r["span_id"] == "s1" and r["change_unit_id"] == "o/r@abc1234def:docs/a.md" and r["output"] == "It runs nightly."
    assert r["input"].startswith("## Fact sheet")
    assert "[chunk 1]\nThe sync job runs nightly at 02:00." in r["context"] and "[chunk 2]\nRetries are capped at five." in r["context"]


def test_build_inputs_reads_both_dataframe_shapes_and_derives_the_change_unit_when_the_attribute_is_missing():
    flat = {"context.span_id": "s2", "attributes.output.value": "text", "attributes.input.value": "in", "attributes.multisync.repo": "o/r", "attributes.multisync.commit": "c1",
            "attributes.multisync.page": "p.md", "attributes.retrieval.documents.1.document.content": "second", "attributes.retrieval.documents.0.document.content": "first"}
    rows, _ = de.build_inputs([flat])
    assert rows[0]["change_unit_id"] == "o/r@c1:p.md" and rows[0]["context"].index("first") < rows[0]["context"].index("second")


def test_build_inputs_skips_what_cannot_be_judged_and_says_why():
    rows, skipped = de.build_inputs([span("a", output=None), span("b", docs=[]), span("c"), {"output.value": "no id", "retrieval.documents": DOCS}])
    assert [r["span_id"] for r in rows] == ["c"]
    assert any("a: no draft text" in s for s in skipped) and any("b: no retrieved chunks" in s for s in skipped) and any("without a span id" in s for s in skipped)


def test_annotations_from_scores_attach_to_the_span_id_and_drop_rows_the_judge_failed_on():
    ok = json.dumps({"name": "faithfulness", "score": 0.0, "label": "unfaithful", "explanation": "invented", "metadata": {"model": "deepseek-v4-pro"}})
    out, failed = de.annotations_from_scores([{"span_id": "s1", "change_unit_id": "u", "faithfulness_score": ok}, {"span_id": "s2", "faithfulness_score": None}], "faithfulness_score")
    assert failed == ["s2"] and out == [{"span_id": "s1", "name": de.EVAL_NAME, "annotator_kind": "LLM", "label": "unfaithful", "score": 0.0, "explanation": "invented",
                                         "metadata": {"judge": "deepseek-v4-pro", "change_unit_id": "u"}}]


def test_flag_low_lists_only_scores_below_the_threshold_worst_first_with_a_link():
    rows = [span("s1"), span("s2", repo="o/q", commit="ffff000", page="x.md"), span("s3")]
    ann = [{"span_id": "s1", "score": 0.7, "label": "faithful", "explanation": None}, {"span_id": "s2", "score": 0.0, "label": "unfaithful", "explanation": "made up"},
           {"span_id": "s3", "score": 0.5, "label": "?", "explanation": None}, {"span_id": "gone", "score": 0.0, "label": "unfaithful", "explanation": None}]
    flagged = de.flag_low(ann, rows, 0.7, {"o/r@abc1234def:docs/a.md": "https://github.com/o/docs/pull/7"})
    assert [f["span_id"] for f in flagged] == ["s2", "s3"], "0.7 is not below 0.7; an unknown span is not listed"
    assert flagged[0]["pr"] == "https://github.com/o/q/commit/ffff000" and flagged[1]["pr"] == "https://github.com/o/docs/pull/7"
    text = de.format_findings(flagged, 0.7, 4)
    assert "o/q@ffff000:x.md" in text and "2 of 4" in text and "made up" in text
    assert "no draft scored below" in de.format_findings([], 0.7, 4)


# ── the judge ────────────────────────────────────────────────────────────────
def test_the_judge_goes_through_the_pipelines_cheap_tier_and_validates_the_label():
    j = PipelineJudge(judge_llm(["faithful", "maybe"]))
    assert j.model == "deepseek-v4-pro"
    assert j.generate_classification("is it?", ["faithful", "unfaithful"]) == {"label": "faithful", "explanation": "because"}
    with pytest.raises(ValueError):
        j.generate_classification([{"role": "user", "content": "is it?"}], {"faithful": "ok", "unfaithful": "no"})


# ── the script ───────────────────────────────────────────────────────────────
ENV = {"PHOENIX_COLLECTOR_ENDPOINT": "http://phoenix.test", "PHOENIX_PROJECT_NAME": "proj", "INTERNAL_AI_API_KEY": "k", "DEEPSEEK_API_KEY": "d"}


def test_zero_draft_spans_exits_0_with_a_clear_message_and_calls_no_model(capsys):
    client = FakeClient([])
    assert run_draft_evals.run([], env=ENV, client=client, judge=object()) == 0
    assert "no draft spans in project proj" in capsys.readouterr().out and client.spans.logged == {}


def test_the_script_scores_every_draft_span_and_logs_one_evaluation_per_original_span_id(capsys):
    client = FakeClient([span("s1"), span("s2", page="b.md"), span("s3", output=None)])
    assert run_draft_evals.run([], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "unfaithful"]))) == 0
    q = client.spans.calls[0]
    assert q["project_identifier"] == "proj" and "name == 'draft'" in json.dumps(q["query"].to_dict()) and (q["end_time"] - q["start_time"]).total_seconds() == 24 * 3600
    assert {k: v["result.score"] for k, v in client.spans.logged.items()} == {"s1": 1.0, "s2": 0.0}
    assert "logged 2 draft_faithfulness evaluations" in capsys.readouterr().out


def test_a_rerun_overwrites_the_same_evaluation_names_on_the_same_span_ids():
    client = FakeClient([span("s1"), span("s2", page="b.md")])
    run_draft_evals.run([], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"])))
    first = set(client.spans.logged)
    run_draft_evals.run([], env=ENV, client=client, judge=PipelineJudge(judge_llm(["unfaithful", "unfaithful"])))
    assert set(client.spans.logged) == first == {"s1", "s2"}
    assert all(v["annotation_name"] == de.EVAL_NAME and v["result.score"] == 0.0 for v in client.spans.logged.values())


def test_dry_run_calls_no_model_and_logs_nothing(capsys):
    client = FakeClient([span("s1")])
    assert run_draft_evals.run(["--dry-run"], env=ENV, client=client, judge=None) == 0
    assert client.spans.logged == {} and "1 evaluable" in capsys.readouterr().out


def test_max_spans_caps_what_reaches_the_judge():
    client = FakeClient([span("s1"), span("s2", page="b.md"), span("s3", page="c.md")])
    run_draft_evals.run(["--max-spans", "2"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"])))
    assert set(client.spans.logged) == {"s1", "s2"}


# ── multisync evals flag-low-scores ─────────────────────────────────────────
def scored_client():
    rows = [span("s1"), span("s2", repo="o/q", commit="ffff000", page="x.md"), span("s3", page="c.md")]
    scores = {"s1": {"annotation_name": de.EVAL_NAME, "result.score": 1.0, "result.label": "faithful", "result.explanation": None},
              "s2": {"annotation_name": de.EVAL_NAME, "result.score": 0.0, "result.label": "unfaithful", "result.explanation": "invented a flag"},
              "s3": {"annotation_name": de.EVAL_NAME, "result.score": 0.6, "result.label": "x", "result.explanation": None}}
    return FakeClient(rows, scores)


def test_flag_low_scores_prints_only_drafts_below_the_threshold_with_change_unit_repo_and_link():
    facts = MemoryFactStore()
    facts.record_review_outcome({"change_unit_id": "o/r@abc1234def:c.md", "repo": "o/r", "diff_classification": "internal", "model_tier_used": "cheap", "outcome": "draft_with_edition",
                                 "policy_version": "v1", "pr_url": "https://github.com/o/docs/pull/9"})
    code, text = evals_cli.run(["flag-low-scores", "--threshold", "0.7"], env=ENV, client=scored_client(), facts=facts)
    assert code == 0 and "2 of 3 scored drafts below 0.7" in text
    assert "o/q@ffff000:x.md" in text and "https://github.com/o/q/commit/ffff000" in text and "invented a flag" in text
    assert "o/r@abc1234def:docs/a.md" not in text and "https://github.com/o/docs/pull/9" in text
    code, text = evals_cli.run(["flag-low-scores", "--threshold", "0.5"], env=ENV, client=scored_client(), facts=facts)
    assert "1 of 3" in text and "o/q@ffff000:x.md" in text and "o/r@abc1234def:c.md" not in text


def test_flag_low_scores_never_opens_a_ticket_without_the_flag_and_opens_one_per_change_unit_with_it():
    transport, calls, _ = rest_desk()
    tickets = create_tickets(REST_CFG, transport)
    _, text = evals_cli.run(["flag-low-scores"], env=ENV, client=scored_client(), facts=MemoryFactStore(), tickets=tickets)
    assert calls == [] and "ticket" not in text
    _, text = evals_cli.run(["flag-low-scores", "--ticket"], env=ENV, client=scored_client(), facts=MemoryFactStore(), tickets=tickets)
    creates = [c for c in calls if c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks")]
    assert len(creates) == 2 and creates[0]["body"]["title"].startswith("[docs-eval] o/q@ffff000 x.md")
    assert "ffff000" in creates[0]["body"]["description"] and "invented a flag" in creates[0]["body"]["description"] and "ticket: https://tickets.example/tasks/42" in text


def test_flag_low_scores_with_no_draft_spans_exits_0():
    code, text = evals_cli.run(["flag-low-scores"], env=ENV, client=FakeClient([]), facts=MemoryFactStore())
    assert code == 0 and "no draft spans" in text


def test_the_multisync_entry_point_dispatches_evals(monkeypatch, capsys):
    from multisync.cli.main import main

    monkeypatch.setattr(evals_cli, "run", lambda argv, **kw: (0, "listed"))
    assert main(["evals", "flag-low-scores"]) == 0 and "listed" in capsys.readouterr().out
