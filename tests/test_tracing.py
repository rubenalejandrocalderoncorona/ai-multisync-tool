import json

import httpx
import pytest

from multisync import tracing
from multisync.config import load_config
from multisync.llm import LLM
from multisync.pipeline import process_change
from multisync.testing import DOC_V1, DOC_V2, PASS_JUDGE, fake_llm, make_deps


@pytest.fixture()
def spans():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    assert tracing.configure(exporter=exp)
    yield exp
    tracing._tracer = tracing._provider = None


def test_tracing_is_a_no_op_until_configured():
    assert not tracing.enabled()
    with tracing.span("x", "CHAIN", a=1) as sp:
        sp.set(b=2)
        sp.io(input="i", output="o")
    tracing.flush()


def test_a_run_is_one_trace_a_root_span_with_a_child_span_per_node(spans):
    d = process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2}, make_deps(llm=fake_llm([PASS_JUDGE])))
    done = {s.name: s for s in spans.get_finished_spans()}
    root = done["docs-sync o/r docs/a.md"]
    assert root.attributes["openinference.span.kind"] == "AGENT" and root.attributes["session.id"] == "test-run"
    assert root.attributes["multisync.outcome"] == d["outcome"] == "pending_review"
    nodes = [s for s in spans.get_finished_spans() if s.name.startswith("node:")]
    assert {s.name for s in nodes} == {f"node:{t['node']}" for t in d["trail"]}
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in nodes), "every node span hangs under the run"
    assert len({s.context.trace_id for s in spans.get_finished_spans()}) == 1
    judge = done["node:judge"]
    assert "precision" in judge.attributes["output.value"] and judge.attributes["multisync.status"] == "ok"


def test_each_model_call_is_an_llm_span_with_messages_tokens_and_cost_under_its_node(spans):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "an answer"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    llm = LLM(load_config({"INTERNAL_AI_API_KEY": "k", "DEEPSEEK_API_KEY": "d"})["ai"], httpx.MockTransport(handler))
    with tracing.span("node:test", "CHAIN"):
        assert llm.chat([{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], tier="cheap") == "an answer"
    finished = {s.name: s for s in spans.get_finished_spans()}
    call = finished["llm:cheap deepseek-v4-pro"]
    a = call.attributes
    assert a["openinference.span.kind"] == "LLM" and a["llm.provider"] == "deepseek" and a["llm.token_count.total"] == 1500
    assert a["llm.input_messages.0.message.role"] == "system" and a["llm.input_messages.1.message.content"] == "hi"
    assert a["llm.output_messages.0.message.content"] == "an answer" and abs(a["multisync.cost_usd"] - (1000 * 0.66 + 500 * 1.98) / 1e6) < 1e-9
    assert call.parent.span_id == finished["node:test"].context.span_id


def test_a_failing_node_marks_its_span_as_an_error_and_the_run_still_raises(spans):
    deps = make_deps()

    def boom(texts):
        raise RuntimeError("embeddings down")

    deps["llm"].embed = boom
    with pytest.raises(RuntimeError):
        process_change({"repo": "o/r", "filePath": "docs/a.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2}, deps)
    bad = next(s for s in spans.get_finished_spans() if s.name == "node:similarity")
    assert bad.status.status_code.name == "ERROR"
