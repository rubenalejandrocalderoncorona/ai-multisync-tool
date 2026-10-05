import json

import httpx
import pytest

from multisync.fallback import escalate
from multisync.pipeline import process_change
from multisync.testing import DOC_V1, DOC_V2, HALLUCINATION_JUDGE, PASS_JUDGE, fake_llm, make_deps


def change(**over):
    return {"repo": "org/svc", "filePath": "docs/api.md", "commit": "abc1234def", "before": DOC_V1, "after": DOC_V2, **over}


def nodes(d):
    return [t["node"] for t in d["trail"]]


def test_happy_path_visits_every_stage_in_order():
    d = process_change(change(), make_deps())
    assert nodes(d) == ["prefilter", "cross_repo", "route", "similarity", "gar", "write_draft", "verify_draft", "judge", "publish"]
    assert all(isinstance(t["ms"], int) and t["runId"] == "test-run" and t["at"] for t in d["trail"])
    assert next(t for t in d["trail"] if t["node"] == "cross_repo")["status"] == "skip"
    assert "precision" in next(t for t in d["trail"] if t["node"] == "judge")["note"]


def test_trivial_diff_stops_after_the_prefilter():
    d = process_change(change(after=DOC_V1 + "x\n"), make_deps())
    assert nodes(d) == ["prefilter"]
    assert d["trail"][0]["status"] == "stop"


def test_exhausted_judge_loop_shows_widen_then_fallback():
    d = process_change(change(), make_deps(llm=fake_llm([HALLUCINATION_JUDGE])))
    n = nodes(d)
    assert "widen" in n
    assert n[-1] == "fallback"
    assert n.count("judge") == 4  # 2 + widened budget of 2 more
    assert n.count("verify_draft") == 4
    assert d["trail"][-1]["status"] == "fallback"


def test_polish_loop_is_visible():
    llm = fake_llm([{**PASS_JUDGE, "quality": 0.3}, PASS_JUDGE])
    n = nodes(process_change(change(), make_deps(llm=llm)))
    assert n[n.index("write_draft"):] == ["write_draft", "verify_draft", "judge", "polish_draft", "judge", "publish"]


def test_logger_receives_one_event_per_node_including_the_fallback_ticket():
    seen = []

    class Logger:
        def log(self, e):
            seen.append(e)

    deps = make_deps(llm=fake_llm([HALLUCINATION_JUDGE]))
    deps["logger"] = Logger()
    deps["escalate"] = lambda d: "https://tickets.example/tasks/9"
    d = process_change(change(), deps)
    assert len(seen) == len(d["trail"])
    assert d["ticket"] == "https://tickets.example/tasks/9"
    assert seen[-1]["note"]["ticket"] == "https://tickets.example/tasks/9"


def test_a_node_that_throws_is_logged_as_an_error_and_the_run_raises():
    seen = []

    class Logger:
        def log(self, e):
            seen.append(e)

    deps = make_deps()

    def boom(texts):
        raise RuntimeError("embeddings down")

    deps["llm"].embed = boom
    deps["logger"] = Logger()
    with pytest.raises(RuntimeError, match="embeddings down"):
        process_change(change(), deps)
    assert seen[-1]["status"] == "error"
    assert seen[-1]["node"] == "similarity"


# ── cAImanDesk ticketing (REST transport against a mock server) ──────────────
def desk(existing=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append({"url": str(request.url), "method": request.method, "body": body, "auth": request.headers.get("authorization")})
        if request.method == "GET" and "/tasks" in request.url.path and "s" in request.url.params:
            return httpx.Response(200, json=existing or [])
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": 42})

    return httpx.MockTransport(handler), calls


ALERTS = {"ticketProvider": "caimandesk", "deskTransport": "rest", "deskBaseUrl": "https://tickets.example", "deskToken": "tok", "deskProjectId": "7"}
FAILURE = {"repo": "org/svc", "path": "docs/a.md", "commit": "c1", "reviewerAction": "auto_rejected", "rootCauseTag": "iteration_cap_exceeded", "reason": "did not converge",
           "attempts": [{"n": 1, "precision": 0.4, "recall": 1, "style": 1, "quality": 1, "failure": "hallucinated_claim"}], "feedback": ["Unsupported claim: <script>"], "draft": "# d"}


def test_desk_creates_a_task_in_the_configured_project_with_bearer_auth_and_escaped_body():
    transport, calls = desk()
    assert escalate(FAILURE, ALERTS, transport) == "https://tickets.example/tasks/42"
    create = next(c for c in calls if c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks"))
    assert create["auth"] == "Bearer tok"
    assert create["body"]["title"] == "[docs-sync] iteration_cap_exceeded: org/svc docs/a.md"
    assert "hallucinated_claim" in create["body"]["description"]
    assert "<script>" not in create["body"]["description"], "untrusted text must be escaped"


def test_desk_repeat_failure_comments_on_the_open_task_instead_of_duplicating():
    title = "[docs-sync] iteration_cap_exceeded: org/svc docs/a.md"
    transport, calls = desk([{"id": 5, "title": title, "done": False}])
    assert escalate(FAILURE, ALERTS, transport) == "https://tickets.example/tasks/5"
    assert any(c["url"].endswith("/tasks/5/comments") for c in calls)
    assert not any(c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks") for c in calls)


def test_desk_closed_task_does_not_suppress_a_new_ticket():
    title = "[docs-sync] iteration_cap_exceeded: org/svc docs/a.md"
    transport, calls = desk([{"id": 5, "title": title, "done": True}])
    assert escalate(FAILURE, ALERTS, transport) == "https://tickets.example/tasks/42"
    assert any(c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks") for c in calls)


def test_desk_unconfigured_token_yields_no_ticket_and_no_network_call():
    transport, calls = desk()
    assert escalate(FAILURE, {**ALERTS, "deskToken": ""}, transport) is None
    assert calls == []
