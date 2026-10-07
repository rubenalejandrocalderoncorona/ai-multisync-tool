import json
import re

import httpx
import pytest

from multisync.fallback import escalate
from multisync.mcpclient import unwrap
from multisync.testing import FakeMcp
from multisync.tickets import create_tickets, review_html, ticket_marker, ticket_ref_from_body, ticket_refs_from_body

ITEMS = [{"path": "overview.md", "precision": 1, "recall": 0.92, "style": 1, "quality": 0.9}]
REVIEW = dict(repo="o/r", commit="abcdef1234", pr_url="https://github.com/o/docs/pull/7", items=ITEMS)


# ── REST transport ────────────────────────────────────────────────────────────
def rest_desk(existing=None):
    calls = []
    tasks = {t["id"]: dict(t) for t in existing or []}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content else None
        url = str(request.url)
        calls.append({"url": url, "method": method, "body": body})
        path = request.url.path
        if method == "GET" and "s" in request.url.params and path.endswith("/tasks"):
            return httpx.Response(200, json=list(tasks.values()))
        if re.search(r"/projects/\d+/tasks$", path) and method == "PUT":
            t = {"id": 42, "done": False, "description": "", **body}
            tasks[42] = t
            return httpx.Response(200, json=t)
        if re.search(r"/tasks/\d+/comments$", path):
            return httpx.Response(200, json={})
        m = re.search(r"/tasks/(\d+)$", path)
        if m:
            tid = int(m.group(1))
            if method == "GET":
                return httpx.Response(200, json=tasks.get(tid) or {"id": tid, "title": "t", "done": False})
            if method == "POST":
                tasks[tid] = {**tasks.get(tid, {}), **body}
                return httpx.Response(200, json=body)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), calls, tasks


REST_CFG = {"deskBaseUrl": "https://tickets.example", "deskToken": "t", "deskProjectId": "7", "deskTransport": "rest"}


def test_rest_open_review_creates_a_docs_review_task_with_the_pr_link_and_scores_and_returns_a_ref():
    transport, calls, _ = rest_desk()
    r = create_tickets(REST_CFG, transport).open_review(**REVIEW)
    assert r["url"] == "https://tickets.example/tasks/42"
    assert r["ref"] == "desk:42"
    create = next(c for c in calls if c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks"))
    assert create["body"]["title"] == "[docs-review] o/r @ abcdef1"
    assert "pull/7" in create["body"]["description"]
    assert "overview.md" in create["body"]["description"]
    assert "merging the pull request" in create["body"]["description"]


def test_rest_a_second_review_for_the_same_commit_adds_a_note_instead_of_a_duplicate():
    transport, calls, _ = rest_desk([{"id": 5, "title": "[docs-review] o/r @ abcdef1", "done": False}])
    r = create_tickets(REST_CFG, transport).open_review(**REVIEW)
    assert r["id"] == 5
    assert r["created"] is False
    assert any(c["url"].endswith("/tasks/5/comments") for c in calls)
    assert not any(c["method"] == "PUT" and c["url"].endswith("/projects/7/tasks") for c in calls)


def test_rest_approve_notes_the_merge_and_closes_the_task_without_clobbering_other_columns():
    transport, calls, tasks = rest_desk([{"id": 5, "title": "x", "done": False, "priority": 2, "description": "keep me"}])
    assert create_tickets(REST_CFG, transport).approve({"id": 5}, "https://github.com/o/docs/pull/7") is True
    assert any(c["url"].endswith("/tasks/5/comments") and "Approved" in c["body"]["comment"] for c in calls)
    assert tasks[5]["done"] is True
    assert tasks[5]["priority"] == 2
    assert tasks[5]["description"] == "keep me"


def test_rest_reject_notes_it_and_leaves_the_task_open():
    transport, _, tasks = rest_desk([{"id": 5, "title": "x", "done": False}])
    create_tickets(REST_CFG, transport).reject({"id": 5}, "https://github.com/o/docs/pull/7")
    assert tasks[5]["done"] is False


def test_unconfigured_means_disabled_and_silent_no_network_call():
    n = {"i": 0}

    def handler(request):
        n["i"] += 1
        return httpx.Response(200)

    t = create_tickets({"deskBaseUrl": "https://x", "deskProjectId": ""}, httpx.MockTransport(handler))
    assert t.enabled is False
    assert t.open_review(**REVIEW) is None
    assert t.open_fallback({"rootCauseTag": "x", "repo": "o/r", "path": "p"}) is None
    assert t.approve({"id": 1}, None) is False
    assert n["i"] == 0


def test_mcp_is_the_default_transport_rest_is_opt_in():
    assert create_tickets({"deskBaseUrl": "x", "deskToken": "t", "deskProjectId": "7", "deskMcpUrl": "http://m/api/v2/mcp"}).transport == "mcp"
    assert create_tickets(REST_CFG).transport == "rest"
    assert create_tickets({"deskBaseUrl": "x", "deskToken": "", "deskProjectId": "7"}).enabled is False, "no token, no tickets"


def test_pr_body_marker_round_trips_and_tolerates_surrounding_text():
    m = ticket_marker({"ref": "desk:42"})
    assert m == "<!-- multisync:ticket=desk:42 -->"
    assert ticket_ref_from_body(f"Intro\n\n{m}\n\nmore") == {"provider": "desk", "id": 42}
    assert ticket_ref_from_body("no marker") is None
    assert ticket_ref_from_body(None) is None


def test_review_ticket_body_escapes_untrusted_text():
    h = review_html("o/<b>r</b>", "abcdef1", "https://x/pull/1?a=1&b=2", [{"path": "<script>.md"}])
    assert "<script>" not in h and "<b>r</b>" not in h
    assert "&amp;b=2" in h


def test_unwrap_structured_content_single_json_text_and_one_text_per_element_lists_all_normalise():
    assert unwrap({"structuredContent": {"result": [{"id": 1}]}}) == [{"id": 1}]
    assert unwrap({"structuredContent": {"id": 2, "title": "t"}}) == {"id": 2, "title": "t"}
    assert unwrap({"content": [{"type": "text", "text": '{"id":3}'}]}) == {"id": 3}
    assert unwrap({"content": [{"type": "text", "text": '{"id":1}'}, {"type": "text", "text": '{"id":2}'}]}) == [{"id": 1}, {"id": 2}]
    assert unwrap({"content": []}) is None
    with pytest.raises(RuntimeError, match="MCP tool error: boom"):
        unwrap({"isError": True, "content": [{"type": "text", "text": "boom"}]})


# ── MCP transport over real HTTP, against an in-process server with Vikunja's native tool names ──
def mcp_cfg(url, token="tok"):
    return {"deskBaseUrl": "https://tickets.example", "deskToken": token, "deskProjectId": "7", "deskMcpUrl": url, "deskTransport": "mcp"}


def test_mcp_review_opens_a_repeat_adds_a_real_comment_approve_comments_and_closes():
    m = FakeMcp()
    try:
        t = create_tickets(mcp_cfg(m.url))
        first = t.open_review(**REVIEW)
        assert first["created"] is True
        assert first["url"] == f"https://tickets.example/tasks/{first['id']}"
        assert next(l for l in m.log if l[0] == "tasks_create")[1]["project_id"] == 7, "project id is a number, as the tool requires"
        assert "pull/7" in m.tasks[first["id"]]["description"]

        again = t.open_review(**REVIEW)
        assert again["id"] == first["id"], "no duplicate task"
        assert again["created"] is False
        assert "Pull request updated" in m.comments[-1]["comment"]

        t.approve({"id": first["id"]}, "https://github.com/o/docs/pull/7")
        assert m.tasks[first["id"]]["done"] is True
        assert "Approved for production" in m.comments[-1]["comment"]
        # a closed task no longer blocks a new ticket for the same title
        assert t.open_review(**REVIEW)["id"] != first["id"]
    finally:
        m.close()


def test_mcp_the_api_token_is_a_bearer_header_a_wrong_token_yields_no_ticket_and_never_raises():
    m = FakeMcp(token="right")
    try:
        assert create_tickets(mcp_cfg(m.url, "right")).open_review(**REVIEW)
        assert escalate({"repo": "o/r", "path": "a.md", "rootCauseTag": "x", "attempts": []}, {"ticketProvider": "caimandesk", "ticketOnFallback": True, **mcp_cfg(m.url, "wrong")}) is None
    finally:
        m.close()


def test_mcp_fallback_tickets_work_and_an_unreachable_server_returns_none():
    m = FakeMcp()
    try:
        url = create_tickets(mcp_cfg(m.url)).open_fallback({"repo": "o/r", "path": "a.md", "commit": "c1", "reviewerAction": "auto_rejected",
                                                             "rootCauseTag": "iteration_cap_exceeded", "reason": "x", "attempts": [], "feedback": ["f"], "draft": "# d"})
        assert re.search(r"tasks/\d+$", url)
    finally:
        m.close()
    assert escalate({"repo": "o/r", "path": "a.md", "rootCauseTag": "x", "attempts": []}, {"ticketProvider": "caimandesk", "ticketOnFallback": True, **mcp_cfg("http://127.0.0.1:9/api/v2/mcp")}) is None


def test_ticket_refs_from_body_a_promotion_pr_carries_one_marker_per_batch_duplicates_collapse():
    body = "Promote\n<!-- multisync:ticket=desk:42 -->\n- a\n<!-- multisync:ticket=desk:43 -->\n<!-- multisync:ticket=desk:42 -->"
    assert ticket_refs_from_body(body) == [{"provider": "desk", "id": 42}, {"provider": "desk", "id": 43}]
    assert ticket_refs_from_body("none") == []


def test_qa_deploy_only_notes_the_ticket_production_approval_closes_it():
    transport, _, tasks = rest_desk([{"id": 5, "title": "x", "done": False, "priority": 2}])
    t = create_tickets(REST_CFG, transport)
    t.qa_deployed({"id": 5}, "https://github.com/o/docs/pull/7", "https://example.org/documentation/qa/")
    assert tasks[5]["done"] is False
    t.approve({"id": 5}, "https://github.com/o/docs/pull/8")
    assert tasks[5]["done"] is True
