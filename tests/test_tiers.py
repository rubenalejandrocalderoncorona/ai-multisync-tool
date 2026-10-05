import json
import re

import httpx

from multisync.config import load_config
from multisync.llm import LLM
from multisync.pipeline import process_change
from multisync.testing import GOOD_DRAFT, PASS_JUDGE, fake_llm, make_deps
from multisync.vectorstore import MemoryVectorStore


def mk(**o):
    env = {"MIN_DIFF_LINES": "1", **(o.pop("env", None) or {})}
    return make_deps(env=env, **o)


def snap(body):
    return f"### FILE: src/a.go\npackage main\n{body}"


V1 = snap('func Alert() {\n  x := 1\n  log("a")\n}\nfunc Silence(id string) {}\n')


def unit(before, after, **o):
    return {"kind": "code", "repo": "o/r", "filePath": "overview.md", "commit": "abc1234", "before": before, "after": after,
            "existing": "# Old\n\nOld text that is long enough to count as an existing page for comparison.\n", "changedFiles": ["src/a.go"],
            "styleKey": "API documentation", "brief": "b", "repoMap": ["src/a.go"], "snapshotFiles": ["src/a.go"], **o}


def internal(v):
    """An internal change that survives the zero-cost prefilter: a new PRIVATE helper (lowercase, not exported) and a changed number."""
    return f"{v}func helper() {{\n  retries := 3\n}}\n"


def tiers_of(llm, kind):
    return [l["tier"] for l in llm.calls["log"] if l["kind"] == kind]


# ── routing by stage ─────────────────────────────────────────────────────────
def test_internals_only_analysis_plan_and_draft_run_cheap_gar_is_cheap_the_judge_stays_expensive():
    llm = fake_llm([PASS_JUDGE])
    d = process_change(unit(V1, internal(V1)), mk(llm=llm))
    assert d["tier"] == "cheap"
    assert next(t for t in d["trail"] if t["node"] == "route")["note"]["tier"] == "cheap"
    assert tiers_of(llm, "analyze") == ["cheap"]
    assert tiers_of(llm, "plan") == ["cheap"]
    assert tiers_of(llm, "draft") == ["cheap"]
    assert tiers_of(llm, "gar") == ["cheap"]
    assert tiers_of(llm, "gar-facts") == ["cheap"]
    assert tiers_of(llm, "judge") == ["expensive"]


def test_public_interface_touched_analysis_plan_and_draft_run_expensive_gar_still_cheap():
    llm = fake_llm([PASS_JUDGE])
    after = V1.replace("func Silence(id string)", "func Silence(id string, reason string)")
    d = process_change(unit(V1, after), mk(llm=llm))
    assert d["tier"] == "expensive"
    assert re.search(r"public interface touched \(1 changed\): Silence", d["route"]["reasons"][0])
    assert tiers_of(llm, "analyze") == ["expensive"]
    assert tiers_of(llm, "draft") == ["expensive"]
    assert tiers_of(llm, "gar") == ["cheap"]
    assert tiers_of(llm, "gar-facts") == ["cheap"]


def test_retrieval_is_not_an_llm_call_embeddings_only_in_the_similarity_stage():
    llm = fake_llm([PASS_JUDGE])
    d = process_change(unit(V1, internal(V1)), mk(llm=llm, vectors=MemoryVectorStore()))
    sim = next(t for t in d["trail"] if t["node"] == "similarity")
    assert "minChunkSimilarity" in sim["note"]
    usage = sim["note"].get("usage") or {}
    assert not usage.get("cheap") and not usage.get("expensive"), "the similarity stage spent no model tokens"


def test_router_force_and_router_judge_override_the_defaults():
    a = fake_llm([PASS_JUDGE])
    process_change(unit(V1, V1.replace("func Silence(id string)", "func Silence()")), mk(llm=a, env={"ROUTER_FORCE": "cheap"}))
    assert tiers_of(a, "draft") == ["cheap"]
    b = fake_llm([PASS_JUDGE])
    process_change(unit(V1, internal(V1)), mk(llm=b, env={"ROUTER_JUDGE": "follow"}))
    assert tiers_of(b, "judge") == ["cheap"], "follow = the judge uses the draft tier"
    c = fake_llm([PASS_JUDGE])
    process_change(unit(V1, internal(V1)), mk(llm=c, env={"ROUTER_JUDGE": "cheap"}))
    assert tiers_of(c, "judge") == ["cheap"]


# ── escalation ───────────────────────────────────────────────────────────────
SHORT = "## Overview\n\nToo short."


def test_escalation_a_cheap_draft_that_fails_the_check_is_redone_once_on_expensive_without_an_attempt_or_a_judge_call():
    llm = fake_llm([PASS_JUDGE], draft=lambda m, o, c: SHORT if (o or {}).get("tier") == "cheap" else GOOD_DRAFT)
    d = process_change(unit(V1, internal(V1)), mk(llm=llm))
    assert d["outcome"] == "pending_review"
    assert d["escalated"] is True
    assert d["tier"] == "expensive"
    assert tiers_of(llm, "draft") == ["cheap", "expensive"]
    names = [t["node"] for t in d["trail"]]
    assert names[names.index("write_draft"):] == ["write_draft", "verify_draft", "write_draft", "verify_draft", "judge", "publish"]
    assert [t for t in d["trail"] if t["node"] == "verify_draft"][0]["status"] == "escalate"
    assert len(d["attempts"]) == 1, "the escalated redo is not counted as an attempt"
    assert llm.calls["judge"] == 1, "the weak draft never cost a judge call"
    assert re.search(r"Fix exactly these problems:[\s\S]*too short", llm.calls["drafts"][1])


def test_escalation_happens_at_most_once_a_failing_expensive_draft_goes_back_to_the_writer():
    llm = fake_llm([PASS_JUDGE], draft=SHORT)
    d = process_change(unit(V1, internal(V1)), mk(llm=llm, env={"MAX_ITERATIONS": "2"}))
    assert d["outcome"] == "fallback"
    assert llm.calls["judge"] == 0
    assert tiers_of(llm, "draft")[:2] == ["cheap", "expensive"]
    assert all(t == "expensive" for t in tiers_of(llm, "draft")[1:])


def test_an_expensive_tier_draft_is_never_escalated_its_failures_are_feedback_for_the_next_attempt():
    n = {"i": 0}

    def draft(m, o, c):
        n["i"] += 1
        return SHORT if n["i"] == 1 else GOOD_DRAFT

    llm = fake_llm([PASS_JUDGE], draft=draft)
    d = process_change(unit(V1, V1.replace("func Silence(id string)", "func Silence()")), mk(llm=llm))
    assert d["escalated"] is False
    assert d["outcome"] == "pending_review"
    assert d["attempts"][0]["failure"] == "deterministic_check"


# ── the client: tiers, endpoints, parameters, cost ───────────────────────────
def recording_transport():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": body})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1]}], "usage": {"total_tokens": 5}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    return httpx.MockTransport(handler), calls


CFG = load_config({"INTERNAL_AI_API_KEY": "sk-openai", "DEEPSEEK_API_KEY": "sk-deepseek"})["ai"]
MSG = [{"role": "user", "content": "x"}]


def test_client_cheap_tier_goes_to_deepseek_expensive_to_openai():
    transport, calls = recording_transport()
    llm = LLM(CFG, transport)
    llm.chat(MSG, tier="cheap")
    llm.chat(MSG, fast=True)
    llm.chat(MSG)
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["auth"] == "Bearer sk-deepseek"
    assert calls[0]["body"]["model"] == "deepseek-v4-pro"
    assert calls[1]["url"] == calls[0]["url"], "`fast=True` is the cheap tier"
    assert calls[2]["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls[2]["auth"] == "Bearer sk-openai"
    assert calls[2]["body"]["model"] == "gpt-5.6-terra"


def test_client_temperature_zero_for_deepseek_omitted_for_gpt5_and_embeddings_go_to_openai():
    transport, calls = recording_transport()
    llm = LLM(CFG, transport)
    llm.chat(MSG, tier="cheap")
    llm.chat(MSG, tier="expensive")
    llm.embed(["text"])
    assert calls[0]["body"]["temperature"] == 0
    assert "temperature" not in calls[1]["body"]
    assert calls[2]["url"] == "https://api.openai.com/v1/embeddings"
    assert calls[2]["auth"] == "Bearer sk-openai"


def test_client_without_a_deepseek_key_the_cheap_tier_falls_back_to_a_small_model_on_the_primary_provider():
    c = load_config({"INTERNAL_AI_API_KEY": "sk-openai"})["ai"]
    assert c["tiers"]["cheap"]["baseUrl"] == "https://api.openai.com"
    assert c["tiers"]["cheap"]["model"] == "gpt-4o-mini"


def test_client_usage_and_cost_are_tracked_per_tier():
    transport, _ = recording_transport()
    llm = LLM(CFG, transport)
    before = llm.snapshot_usage()
    llm.chat(MSG, tier="cheap")
    llm.chat(MSG, tier="expensive")
    u = llm.snapshot_usage()
    assert u["cheap"]["calls"] == 1
    assert u["cheap"]["in"] == 1000
    assert abs(u["cheap"]["usd"] - (1000 * 0.66 + 500 * 1.98) / 1e6) < 1e-9, "DeepSeek pro estimate"
    assert abs(u["expensive"]["usd"] - (1000 * 2 + 500 * 12) / 1e6) < 1e-9, "terra estimate"
    assert before["cheap"]["calls"] == 0, "a snapshot is a copy, not a live view"


def test_graph_the_decision_carries_a_cost_summary_split_by_tier_and_node_notes_carry_their_own_usage():
    transport, _ = recording_transport()
    real = LLM(CFG, transport)
    fake = fake_llm([PASS_JUDGE])

    class Wrapped:
        """Scripted answers from the fake, token accounting from the real client."""

        calls = fake.calls

        def snapshot_usage(self):
            return real.snapshot_usage()

        def chat(self, m, **kw):
            real.chat(m, **kw)
            return fake.chat(m, **kw)

        def chat_json(self, m, **kw):
            real.chat(m, **kw)
            return fake.chat_json(m, **kw)

        def embed(self, texts):
            return fake.embed(texts)

    d = process_change(unit(V1, internal(V1)), mk(llm=Wrapped()))
    assert d["cost"]["cheap"]["calls"] >= 3 and d["cost"]["expensive"]["calls"] >= 1
    assert d["cost"]["usd"] > 0
    assert any((t["note"].get("usage") or {}).get("cheap") for t in d["trail"])
    assert d["cost"]["usd"] == round(d["cost"]["cheap"]["usd"] + d["cost"]["expensive"]["usd"], 4)
