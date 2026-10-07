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


REPLIES = {"faithful": json.dumps({"claims": 4, "unsupported": [], "explanation": "because"}),
           "unfaithful": json.dumps({"claims": 4, "unsupported": ["q1", "q2", "q3", "q4"], "explanation": "because"})}


def judge_llm(verdicts):
    """A cheap-tier model that answers with the next reply: 'faithful' / 'unfaithful' (strict JSON) or any raw text."""
    seen = iter(verdicts)
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        v = next(seen)
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLIES.get(v, v)}}], "usage": {}})

    llm = LLM(load_config({"INTERNAL_AI_API_KEY": "k", "DEEPSEEK_API_KEY": "d"})["ai"], httpx.MockTransport(handler))
    llm.prompts = prompts
    return llm


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
    res = de.parse_verdict('{"claims": 4, "unsupported": ["invented flag"], "explanation": "one"}')
    res["model"] = "deepseek-v4-pro"
    out, failed = de.annotations_from_scores([{"span_id": "s1", "change_unit_id": "u", "evidence": "source+chunks", "evidence_chars": 99, "faithfulness_score": res},
                                              {"span_id": "s2", "faithfulness_score": None}], "faithfulness_score")
    assert failed == ["s2"] and len(out) == 1
    a = out[0]
    assert (a["span_id"], a["name"], a["label"], a["score"]) == ("s1", de.EVAL_NAME, "partial", 0.75) and a["explanation"] == "one"
    assert a["metadata"] == {"judge": "deepseek-v4-pro", "change_unit_id": "u", "claims": 4, "unsupported": 1, "quotes": ["invented flag"], "evidence": "source+chunks", "evidence_chars": 99, "notes": []}


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
    j = PipelineJudge(judge_llm(['{"label": "faithful", "explanation": "because"}', '{"label": "maybe"}']))
    assert j.model == "deepseek-v4-pro"
    assert j.generate_classification("is it?", ["faithful", "unfaithful"]) == {"label": "faithful", "explanation": "because"}
    with pytest.raises(ValueError):
        j.generate_classification([{"role": "user", "content": "is it?"}], {"faithful": "ok", "unfaithful": "no"})


# ── the script ───────────────────────────────────────────────────────────────
ENV = {"PHOENIX_COLLECTOR_ENDPOINT": "http://phoenix.test", "PHOENIX_PROJECT_NAME": "proj", "INTERNAL_AI_API_KEY": "k", "DEEPSEEK_API_KEY": "d"}


def test_zero_draft_spans_exits_0_with_a_clear_message_and_calls_no_model(capsys):
    client = FakeClient([])
    assert run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=object()) == 0
    assert "no draft spans in project proj" in capsys.readouterr().out and client.spans.logged == {}


def test_the_script_scores_every_draft_span_and_logs_one_evaluation_per_original_span_id(capsys):
    client = FakeClient([span("s1"), span("s2", page="b.md"), span("s3", output=None)])
    assert run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "unfaithful"]))) == 0
    q = client.spans.calls[0]
    assert q["project_identifier"] == "proj" and "name == 'draft'" in json.dumps(q["query"].to_dict()) and (q["end_time"] - q["start_time"]).total_seconds() == 24 * 3600
    assert {k: v["result.score"] for k, v in client.spans.logged.items()} == {"s1": 1.0, "s2": 0.0}
    assert "logged 2 draft_faithfulness evaluations" in capsys.readouterr().out


def test_a_rerun_overwrites_the_same_evaluation_names_on_the_same_span_ids():
    client = FakeClient([span("s1"), span("s2", page="b.md")])
    run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"])))
    first = set(client.spans.logged)
    run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["unfaithful", "unfaithful"])))
    assert set(client.spans.logged) == first == {"s1", "s2"}
    assert all(v["annotation_name"] == de.EVAL_NAME and v["result.score"] == 0.0 for v in client.spans.logged.values())


def test_dry_run_calls_no_model_and_logs_nothing(capsys):
    client = FakeClient([span("s1")])
    assert run_draft_evals.run(["--include-chunks-only", "--dry-run"], env=ENV, client=client, judge=None) == 0
    out = capsys.readouterr().out
    assert client.spans.logged == {} and "1 evaluable" in out and "evidence chunks_only" in out and "s1" in out


def test_max_spans_caps_what_reaches_the_judge():
    client = FakeClient([span("s1"), span("s2", page="b.md"), span("s3", page="c.md")])
    run_draft_evals.run(["--include-chunks-only", "--max-spans", "2"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"])))
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


# ── evidence parity, strict judge, calibration ──────────────────────────────
SNAPSHOT = ("### FILE: receiver/webhook.py\nThe webhook receiver uses only the standard library: http.server, no httpx.\n\n"
            "### FILE: multisync/pipeline.py\nThe pipeline is a LangGraph state graph and calls models with httpx.\n\n")


def with_source(row, snapshot=SNAPSHOT, changed=("multisync/pipeline.py",), **kw):
    return {**row, **tracing.source_attrs(snapshot, list(changed), **kw)}


def judge_by_evidence():
    """A fake judge that only 'knows' what is in the prompt: the claim is supported when the evidence part contains the words it relies on."""
    class J:
        model = "fake"

        def complete(self, prompt):
            evidence = prompt.split("=== EVIDENCE ===")[1].split("=== DRAFT TO CHECK ===")[0]
            ok = "LangGraph state graph" in evidence
            return json.dumps({"claims": 2, "unsupported": [] if ok else ["The system uses LangGraph and httpx"], "explanation": "x"})
    return J()


def test_source_attrs_are_bounded_changed_files_first_and_hash_the_whole_snapshot():
    big = "### FILE: a.py\n" + "x" * 500 + "\n\n### FILE: b.py\n" + "y" * 500 + "\n"
    a = tracing.source_attrs(big, ["b.py"], budget=300, capture=True)
    assert a["multisync.source.chars"] == len(big) and len(a["multisync.source.sha256"]) == 64
    assert json.loads(a["multisync.source.files"]) == ["a.py", "b.py"] and json.loads(a["multisync.source.changed_files"]) == ["b.py"]
    text = a["multisync.source.text"]
    assert text.startswith("### FILE: b.py") and "y" * 100 in text and "x" * 50 not in text and "more characters not recorded" in text and len(text) < 450
    assert tracing.source_attrs("short", budget=300, capture=True)["multisync.source.text"].startswith("### FILE: (source)")


def test_content_capture_switch_off_records_no_source_text_only_paths_and_a_hash(monkeypatch):
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "0")
    assert tracing.content_capture_enabled() is False
    a = tracing.source_attrs(SNAPSHOT, ["multisync/pipeline.py"])
    assert "multisync.source.text" not in a and a["multisync.source.sha256"] and "receiver/webhook.py" in a["multisync.source.files"]
    monkeypatch.delenv("PHOENIX_CAPTURE_CONTENT")
    assert tracing.content_capture_enabled() is True and "multisync.source.text" in tracing.source_attrs(SNAPSHOT)


def test_the_budget_env_knob_sets_the_default(monkeypatch):
    monkeypatch.setenv("DRAFT_SOURCE_CHARS", "40")
    assert len(tracing.source_attrs("### FILE: a\n" + "z" * 500, capture=True)["multisync.source.text"]) < 120
    monkeypatch.setenv("DRAFT_SOURCE_CHARS", "junk")
    assert tracing.source_budget() == tracing.SOURCE_BUDGET


def test_the_pipeline_draft_span_records_the_source_and_respects_the_privacy_switch(spans, monkeypatch):
    change = {"repo": "o/r", "filePath": "docs/a.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2}
    process_change(change, make_deps(llm=fake_llm([PASS_JUDGE])))
    a = next(s for s in spans.get_finished_spans() if s.name == "draft").attributes
    assert a["multisync.source.text"].strip() and a["multisync.source.sha256"] and a["multisync.source.chars"] == len(DOC_V2)
    spans.clear()
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "0")
    process_change(change, make_deps(llm=fake_llm([PASS_JUDGE])))
    a = next(s for s in spans.get_finished_spans() if s.name == "draft").attributes
    assert "multisync.source.text" not in a and a["multisync.source.sha256"]


def test_evidence_includes_the_source_the_chunks_and_flags_the_kind():
    rows, _ = de.build_inputs([with_source(span("s1"))])
    r = rows[0]
    assert r["evidence"] == "source+chunks" and r["source_files"] == 2 and r["chunks"] == 2
    assert "[SOURCE FILES (changed in this commit: multisync/pipeline.py)]" in r["context"] and "[chunk 1]" in r["context"]
    assert r["evidence_chars"] == len(r["context"]) and "documents this component only" in r["prompt"] and "docs/a.md" in r["prompt"] and "o/r" in r["prompt"]


def test_privacy_off_span_degrades_to_chunks_only_and_the_annotation_says_so(monkeypatch):
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "0")
    rows, _ = de.build_inputs([with_source(span("s1"))])
    assert rows[0]["evidence"] == "chunks_only" and rows[0]["source_hash_only"] and "SOURCE FILES" not in rows[0]["context"]
    ann, _ = de.annotations_from_scores(de.evaluate(rows, judge_by_evidence())[0], "faithfulness_score")
    assert ann[0]["metadata"]["evidence"] == "chunks_only" and "source_not_recorded" in ann[0]["metadata"]["notes"]


def test_old_spans_without_the_new_attributes_still_evaluate_on_chunks_only():
    rows, skipped = de.build_inputs([span("old")])
    assert skipped == [] and rows[0]["evidence"] == "chunks_only"
    ann, failed = de.annotations_from_scores(de.evaluate(rows, PipelineJudge(judge_llm(["faithful"])))[0], "faithfulness_score")
    assert failed == [] and ann[0]["metadata"]["evidence"] == "chunks_only" and "old_span_without_source" in ann[0]["metadata"]["notes"] and ann[0]["score"] == 1.0


def test_a_source_only_span_without_chunks_is_evaluable():
    rows, _ = de.build_inputs([with_source(span("s1", docs=[]))])
    assert len(rows) == 1 and rows[0]["evidence"] == "source+chunks" and rows[0]["chunks"] == 0


def test_false_positive_a_statement_supported_only_by_the_source_snapshot_is_faithful_with_it_and_unfaithful_without():
    """Production false positive: the chunks come from another component (the webhook receiver, stdlib only); what supports the page's claim is in the source."""
    other_component_chunks = [{"document.content": "The webhook receiver uses only the standard library."}]
    base = span("s1", output="The system is a LangGraph state graph that uses httpx.", docs=other_component_chunks)
    judge = judge_by_evidence()
    for row, expected in ((with_source(base), ("faithful", 1.0)), (base, ("unfaithful", 0.5))):
        built, _ = de.build_inputs([row])
        ann, _ = de.annotations_from_scores(de.evaluate(built, judge)[0], "faithfulness_score")
        assert (ann[0]["label"], ann[0]["score"]) == expected
    assert ann[0]["metadata"]["quotes"] == ["The system uses LangGraph and httpx"] and ann[0]["metadata"]["evidence"] == "chunks_only"


def test_parse_verdict_accepts_fenced_json_and_computes_the_share_of_supported_claims():
    r = de.parse_verdict('```json\n{"claims": 10, "unsupported": ["a", "b"], "explanation": "e"}\n```')
    assert (r["claims"], r["unsupported_count"], r["score"], r["label"], r["unsupported"]) == (10, 2, 0.8, "partial", ["a", "b"])
    assert de.parse_verdict('Sure: {"claims": 3, "unsupported": []}')["label"] == "faithful"
    assert de.parse_verdict('{"claims": 0, "unsupported": []}')["notes"] == ["no_claims_judged"]
    assert de.parse_verdict('{"claims": 2, "unsupported": 1}')["score"] == 0.5


def test_parse_verdict_maps_a_bare_label_with_a_note_and_rejects_malformed_replies():
    r = de.parse_verdict("unfaithful")
    assert (r["score"], r["label"], r["notes"], r["claims"]) == (0.0, "unfaithful", ["judge_replied_with_label_only"], None)
    assert de.parse_verdict("`Faithful`.")["score"] == 1.0
    for bad in ("", "I think it is mostly fine", '{"claims": "many"}', '{"label": "faithful"}', "{broken"):
        with pytest.raises(ValueError):
            de.parse_verdict(bad)


def test_label_thresholds():
    assert [de.label_for(x) for x in (1.0, 0.9, 0.89, 0.7, 0.69, 0.0)] == ["faithful", "faithful", "partial", "partial", "unfaithful", "unfaithful"]


def test_one_borderline_claim_no_longer_flips_the_whole_draft():
    r = de.parse_verdict('{"claims": 12, "unsupported": ["one iffy claim"]}')
    assert r["score"] > 0.9 and r["label"] == "faithful"


def test_the_judge_is_retried_once_on_a_malformed_reply_then_given_up_on():
    llm = judge_llm(["not json at all", REPLIES["faithful"]])
    rows, _ = de.build_inputs([span("s1")])
    res = de.judge_row(rows[0], PipelineJudge(llm))
    assert res["score"] == 1.0 and len(llm.prompts) == 2 and "previous reply was not the required JSON" in llm.prompts[1]
    llm = judge_llm(["nope", "still nope", REPLIES["faithful"]])
    assert de.judge_row(rows[0], PipelineJudge(llm)) is None and len(llm.prompts) == 2


def test_a_bare_label_is_accepted_without_any_retry():
    llm = judge_llm(["Unfaithful."])
    res = de.judge_row(de.build_inputs([span("s1")])[0][0], PipelineJudge(llm))
    assert res["notes"] == ["judge_replied_with_label_only"] and len(llm.prompts) == 1


def test_sample_dry_run_prints_evidence_sizes_calls_no_model_and_writes_nothing(capsys):
    client = FakeClient([with_source(span(f"s{i}", page=f"p{i}.md")) for i in range(6)] + [span("old", page="old.md")])
    assert run_draft_evals.run(["--sample", "3", "--dry-run"], env=ENV, client=client, judge=None) == 0
    out = capsys.readouterr().out
    assert "3 evaluable" in out and "evidence source+chunks" in out and "chars (2 source files" in out and client.spans.logged == {}


def test_explain_prints_scores_with_quotes_and_never_logs(capsys):
    client = FakeClient([with_source(span("s1"))])
    reply = json.dumps({"claims": 5, "unsupported": ["it needs Redis"], "explanation": "Redis is not mentioned"})
    assert run_draft_evals.run(["--explain"], env=ENV, client=client, judge=PipelineJudge(judge_llm([reply]))) == 0
    out = capsys.readouterr().out
    assert "0.80 partial" in out and "unsupported: it needs Redis" in out and "evidence source+chunks" in out and client.spans.logged == {}


def test_the_script_logs_label_counts_and_quotes_in_the_metadata():
    captured = []
    client = FakeClient([with_source(span("s1")), span("old", page="o.md")])
    reply = json.dumps({"claims": 4, "unsupported": ["bad claim"]})
    run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=PipelineJudge(judge_llm([reply, reply])), log=lambda c, ann: captured.extend(ann) or len(ann))
    by = {a["span_id"]: a for a in captured}
    assert by["s1"]["label"] == "partial" and by["s1"]["score"] == 0.75 and by["s1"]["metadata"]["claims"] == 4 and by["s1"]["metadata"]["unsupported"] == 1
    assert by["s1"]["metadata"]["quotes"] == ["bad claim"] and by["s1"]["metadata"]["evidence"] == "source+chunks"
    assert by["old"]["metadata"]["evidence"] == "chunks_only"


def test_flag_low_scores_shows_quotes_counts_and_the_weaker_chunks_only_note():
    meta = {"claims": 4, "unsupported": 3, "quotes": ["invented flag --x"], "evidence": "chunks_only"}
    client = FakeClient([span("s1")], {"s1": {"annotation_name": de.EVAL_NAME, "result.score": 0.25, "result.label": "unfaithful", "result.explanation": "bad", "metadata": meta}})
    code, text = evals_cli.run(["flag-low-scores"], env=ENV, client=client, facts=MemoryFactStore())
    assert code == 0 and "3 of 4 claims unsupported" in text and "weaker score: chunks only" in text and "quote  invented flag --x" in text
    code, text = evals_cli.run(["flag-low-scores"], env={**ENV, "DRAFT_EVALS_THRESHOLD": "0.2"}, client=client, facts=MemoryFactStore())
    assert "no draft scored below 0.2" in text


def test_spans_without_source_text_are_left_unscored_by_default_and_judged_only_when_asked(capsys):
    client = FakeClient([span("s1"), span("s2", page="b.md")])
    assert run_draft_evals.run([], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"]))) == 0
    out = capsys.readouterr().out
    assert "0 evaluable" in out and "2 left unscored" in out and client.spans.logged == {}
    run_draft_evals.run(["--include-chunks-only"], env=ENV, client=client, judge=PipelineJudge(judge_llm(["faithful", "faithful"])))
    assert len(client.spans.logged) == 2
