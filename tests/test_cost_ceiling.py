"""Cost ceilings: MAX_PAGE_USD stops a looping page between attempts, MAX_RUN_USD stops the run before the next page. Safety nets only."""
import copy
import json
import os

from multisync.config import load_config, policy_version
from multisync.pipeline import process_change
from multisync.testing import DOC_V1, DOC_V2, HALLUCINATION_JUDGE, PASS_JUDGE, FakeLLM, FakeOpenAI, fake_llm, make_deps
from tests.test_e2e_cli import base_env, commit, init_repo, run_cli


class PricedLLM(FakeLLM):
    """FakeLLM with usage accounting: every chat call costs `per_call` dollars on its tier."""

    def __init__(self, per_call=0.1, **kw):
        super().__init__(**kw)
        self.per_call = per_call
        self.usage = {"cheap": {"calls": 0, "in": 0, "out": 0, "usd": 0.0}, "expensive": {"calls": 0, "in": 0, "out": 0, "usd": 0.0}, "embed": {"calls": 0, "tokens": 0}}

    def snapshot_usage(self):
        return copy.deepcopy(self.usage)

    def _bill(self, tier, fast):
        slot = self.usage["cheap" if tier == "cheap" or fast else "expensive"]
        slot["calls"] += 1
        slot["usd"] += self.per_call

    def chat(self, messages, tier=None, fast=False, temperature=...):
        self._bill(tier, fast)
        return super().chat(messages, tier, fast, temperature)

    def chat_json(self, messages, tier=None, fast=False, temperature=...):
        self._bill(tier, fast)
        return super().chat_json(messages, tier, fast, temperature)


def change(**over):
    return {"repo": "org/svc", "filePath": "docs/api.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2, **over}


def priced(per_call, judges, **env):
    llm = PricedLLM(per_call=per_call, judges=judges)
    return make_deps(env={"MAX_ITERATIONS": "3", **env}, llm=llm), llm


def test_page_ceiling_stops_a_looping_page_after_the_attempt_that_crosses_it_and_keeps_the_draft():
    deps, llm = priced(0.1, [HALLUCINATION_JUDGE], MAX_PAGE_USD="0.5")
    d = process_change(change(), deps)
    assert d["outcome"] == "fallback" and d["rootCauseTag"] == "cost_ceiling_page"
    assert d["draft"] and d["feedback"]
    assert d["reason"].startswith("page spent $") and "of the $0.5 page ceiling after" in d["reason"] and "last check:" in d["reason"]
    assert d["trail"][-1]["node"] == "cost_stop" and d["trail"][-1]["status"] == "fallback"
    assert [t["node"] for t in d["trail"]].count("judge") < 6, "the loop was cut short"
    assert d["metrics"]["costUsd"] == d["cost"]["usd"] and d["cost"]["usd"] >= 0.5
    assert d["ticket"] is None


def test_page_under_the_ceiling_is_untouched():
    deps, _ = priced(0.01, [PASS_JUDGE], MAX_PAGE_USD="1.5")
    d = process_change(change(), deps)
    base = process_change(change(), make_deps(env={"MAX_ITERATIONS": "3"}, llm=fake_llm([PASS_JUDGE])))
    assert d["outcome"] == base["outcome"] == "pending_review"
    assert d["attempts"] == base["attempts"] and len(d["content"]) == len(base["content"])
    assert [t["node"] for t in d["trail"]] == [t["node"] for t in base["trail"]]


def test_a_passing_attempt_that_crosses_the_ceiling_still_publishes():
    deps, _ = priced(1.0, [PASS_JUDGE], MAX_PAGE_USD="0.5")
    assert process_change(change(), deps)["outcome"] == "pending_review"


def test_page_ceiling_zero_disables_it():
    deps, _ = priced(1.0, [HALLUCINATION_JUDGE], MAX_PAGE_USD="0")
    assert process_change(change(), deps)["rootCauseTag"] == "iteration_cap_exceeded"


def test_page_spend_is_measured_per_page_not_per_run():
    deps, llm = priced(0.1, [PASS_JUDGE], MAX_PAGE_USD="1.5")
    llm.usage["expensive"]["usd"] = 50.0  # earlier pages of the run
    assert process_change(change(), deps)["outcome"] == "pending_review"


def test_defaults_and_policy_version():
    t = load_config({})["thresholds"]
    assert t["maxPageUsd"] == 1.5 and t["maxRunUsd"] == 6
    assert policy_version(load_config({})) != policy_version(load_config({"MAX_PAGE_USD": "3"}))
    assert policy_version(load_config({})) != policy_version(load_config({"MAX_RUN_USD": "0"}))


def test_a_ceiling_fallback_opens_no_ticket():
    deps, _ = priced(0.1, [HALLUCINATION_JUDGE], MAX_PAGE_USD="0.5")
    seen = []
    deps["escalate"] = lambda dec: seen.append(dec)
    d = process_change(change(), deps)
    assert d["ticket"] is None and seen == []


def test_run_ceiling_skips_later_pages_without_any_model_call(tmp_path):
    fake = FakeOpenAI()
    src = tmp_path / "source-repo"
    try:
        init_repo(src)
        (src / "src/a.go").write_text("package main\nfunc Alert() {}\n")
        commit(src, "one")
        (src / "src/a.go").write_text('package main\nfunc Alert() {}\nfunc Silence(id string) {}\nconst Port = 8081\n')
        c2 = commit(src, "two")
        pages = [{"path": "a.md", "kind": "API documentation", "scope": ["src/**"]}, {"path": "b.md", "kind": "API documentation", "scope": ["src/**"]}]
        env = base_env(tmp_path, fake.url, {"repos": {"o/proj": {"mode": "code", "trust": "auto", "serviceName": "proj", "pages": pages}}},
                       SOURCE_SHA=c2, FULL_SYNC="1", AI_EXPENSIVE_PRICE_IN="100", AI_EXPENSIVE_PRICE_OUT="0", AI_CHEAP_PRICE_IN="100", AI_CHEAP_PRICE_OUT="0",MAX_RUN_USD="0.5", MAX_PAGE_USD="0")
        code, out = run_cli(tmp_path, env)
        assert code == 0, out
        by = {r["path"]: r for r in json.loads((tmp_path / "pipeline-results.json").read_text())["results"]}
        assert by["a.md"].get("rootCauseTag") != "cost_ceiling_run" and by["a.md"]["costUsd"] > 0
        assert by["b.md"]["outcome"] == "fallback" and by["b.md"]["rootCauseTag"] == "cost_ceiling_run" and by["b.md"]["stages"] == []
        assert "1 pages not attempted: run ceiling $0.5 reached" in out
        assert "spend: run $" in out
        assert not os.path.exists(tmp_path / "rejected" / "b.md")
    finally:
        fake.close()
