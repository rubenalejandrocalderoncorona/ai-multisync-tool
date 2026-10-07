import json

import httpx
import pytest

from multisync.chunker import point_id
from multisync.config import load_config, repo_policy
from multisync.critic import evaluate
from multisync.fallback import escalate
from multisync.llm import LLM, parse_json, retry_delay_ms
from multisync.vectorstore import QdrantStore


def recorder(responder):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "method": request.method, "body": json.loads(request.content) if request.content else None, "headers": dict(request.headers)})
        status, payload = responder(str(request.url), request)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), calls


def test_qdrant_creates_collection_with_cosine_distance_and_payload_indexes_when_missing():
    transport, calls = recorder(lambda url, r: (404, {}) if url.endswith("/collections/docs") and r.method == "GET" else (200, {"result": True}))
    q = QdrantStore({"url": "http://q:6333", "apiKey": "k", "collection": "docs"}, 1536, transport)
    q.ensure_collection()
    put = next(c for c in calls if c["method"] == "PUT" and c["url"].endswith("/collections/docs"))
    assert put["body"] == {"vectors": {"size": 1536, "distance": "Cosine"}}
    assert put["headers"]["api-key"] == "k"
    assert len([c for c in calls if c["url"].endswith("/index")]) == 4


def test_qdrant_search_scopes_to_repo_and_maps_results():
    transport, calls = recorder(lambda url, r: (200, {"result": [{"id": "a", "score": 0.9, "payload": {"text": "t"}}]}))
    q = QdrantStore({"url": "http://q:6333", "collection": "docs"}, 3, transport)
    out = q.search([1, 0, 0], limit=2, repo="org/svc")
    assert out == [{"id": "a", "score": 0.9, "payload": {"text": "t"}}]
    assert calls[0]["body"]["filter"] == {"must": [{"key": "repo", "match": {"value": "org/svc"}}]}


def test_qdrant_touch_commit_rekeys_payload_without_reembedding():
    transport, calls = recorder(lambda url, r: (200, {}))
    QdrantStore({"url": "http://q", "collection": "docs"}, 3, transport).touch_commit(["a", "b"], "c9")
    assert calls[0]["body"]["payload"]["commit"] == "c9"
    assert calls[0]["body"]["points"] == ["a", "b"]


def test_point_id_is_deterministic_and_uuid_shaped():
    assert point_id("r", "p", 0) == point_id("r", "p", 0)
    assert point_id("r", "p", 0) != point_id("r", "p", 1)
    import re
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", point_id("r", "p", 0))


def test_fallback_opens_a_github_issue_with_the_failed_checks_and_pings_slack():
    transport, calls = recorder(lambda url, r: (200, {"html_url": "https://gh/issue/1"}) if "api.github.com" in url else (200, {}))
    ticket = escalate(
        {"repo": "org/svc", "path": "docs/a.md", "commit": "c1", "reviewerAction": "auto_rejected", "rootCauseTag": "iteration_cap_exceeded", "reason": "no converge",
         "attempts": [{"n": 1, "precision": 0.5, "recall": 1, "style": 1, "quality": 1, "failure": "hallucinated_claim"}], "feedback": ["Unsupported claim: x"], "draft": "# d"},
        {"ticketProvider": "github", "ticketOnFallback": True, "githubToken": "t", "githubRepo": "org/central", "slackWebhook": "https://hooks/slack"}, transport)
    assert ticket == "https://gh/issue/1"
    issue = next(c for c in calls if "/repos/org/central/issues" in c["url"])
    assert "iteration_cap_exceeded" in issue["body"]["body"]
    assert "Unsupported claim: x" in issue["body"]["body"]
    assert any(c["url"] == "https://hooks/slack" for c in calls)


def test_fallback_never_raises_when_delivery_fails():
    def boom(request):
        raise httpx.ConnectError("network down")

    t = escalate({"repo": "o/r", "path": "p", "rootCauseTag": "x", "attempts": []},
                 {"ticketProvider": "github", "ticketOnFallback": True, "githubToken": "t", "githubRepo": "o/c", "slackWebhook": "https://h"}, httpx.MockTransport(boom))
    assert t is None


def test_critic_hallucination_outranks_every_other_failure():
    t = load_config({})["thresholds"]
    r = evaluate({"precision": 0.5, "recall": 0.1, "style": 0.1, "quality": 0.1, "unsupported": ["bad"], "missing": ["m"], "notes": []}, t)
    assert r["tag"] == "hallucinated_claim"
    assert "bad" in r["feedback"][0]


def test_critic_passes_only_when_every_metric_clears_its_threshold():
    t = load_config({})["thresholds"]
    assert evaluate({"precision": 1, "recall": 1, "style": 0.9, "quality": 0.9, "unsupported": [], "missing": [], "notes": []}, t) is None
    assert evaluate({"precision": 1, "recall": 0.5, "style": 0.9, "quality": 0.9, "unsupported": [], "missing": ["f"], "notes": []}, t)["tag"] == "missing_claim"


AI = {"baseUrl": "http://x", "chatPath": "/v1/chat/completions", "embedPath": "/v1/embeddings", "apiKey": "sk", "model": "m", "fastModel": "fm", "embedModel": "e", "timeoutMs": 1000}


def test_llm_client_sends_bearer_auth_parses_fenced_json_orders_embeddings_by_index():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 1, "embedding": [2]}, {"index": 0, "embedding": [1]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"a":1}\n```'}}]})

    llm = LLM(AI, httpx.MockTransport(handler))
    assert llm.chat_json([{"role": "user", "content": "hi"}]) == {"a": 1}
    assert seen["auth"] == "Bearer sk"
    assert llm.embed(["a", "b"]) == [[1], [2]]
    assert parse_json('noise {"z":2} tail') == {"z": 2}


def test_repo_policy_unknown_repos_default_to_review():
    assert repo_policy({"defaults": {"trust": "review"}, "repos": {"o/trusted": {"trust": "auto"}}}, "o/unknown")["trust"] == "review"
    assert repo_policy({"defaults": {}, "repos": {"o/trusted": {"trust": "auto"}}}, "o/trusted")["trust"] == "auto"


# ── LLM retries ──────────────────────────────────────────────────────────────
AI_R = {**AI, "baseUrl": "http://x", "chatPath": "/c", "embedPath": "/e", "apiKey": "k", "maxRetries": 3}


def resp(status, body="", headers=None):
    return httpx.Response(status, content=body if isinstance(body, str) else json.dumps(body), headers=headers or {})


def test_llm_retry_429_with_try_again_in_waits_that_long_plus_margin_then_succeeds():
    waits, n = [], {"i": 0}

    def handler(request):
        n["i"] += 1
        if n["i"] <= 2:
            return resp(429, {"error": {"message": "Rate limit reached. Please try again in 25.888s."}})
        return resp(200, {"choices": [{"message": {"content": "ok"}}]})

    llm = LLM(AI_R, httpx.MockTransport(handler), sleep=lambda s: waits.append(round(s * 1000)))
    assert llm.chat([{"role": "user", "content": "x"}]) == "ok"
    assert n["i"] == 3
    assert waits == [26638, 26638]


def test_llm_retry_honors_retry_after_backs_off_exponentially_and_caps_the_wait():
    assert retry_delay_ms(resp(429, "", {"retry-after": "7"}), "", 0) == 7000
    assert retry_delay_ms(resp(500), "oops", 0) == 1000
    assert retry_delay_ms(resp(500), "oops", 3) == 8000
    assert retry_delay_ms(resp(500), "oops", 10) == 30000
    assert retry_delay_ms(resp(429), "try again in 800ms", 0) == 1550
    assert retry_delay_ms(resp(429, "", {"retry-after": "9999"}), "", 0) == 90000


def test_llm_retry_5xx_and_network_errors_retried_400_401_not_gives_up_after_max_retries():
    n = {"i": 0}

    def flaky(request):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("fetch failed")
        if n["i"] == 2:
            return resp(503, "unavailable")
        return resp(200, {"data": [{"index": 0, "embedding": [1]}]})

    assert LLM(AI_R, httpx.MockTransport(flaky), sleep=lambda s: None).embed(["a"]) == [[1]]
    assert n["i"] == 3

    bad = {"i": 0}

    def unauthorized(request):
        bad["i"] += 1
        return resp(401, "nope")

    with pytest.raises(RuntimeError, match="AI API 401"):
        LLM(AI_R, httpx.MockTransport(unauthorized), sleep=lambda s: None).chat([{"role": "user", "content": "x"}])
    assert bad["i"] == 1, "a 401 is never retried"

    always = {"i": 0}

    def slow(request):
        always["i"] += 1
        return resp(429, "slow down")

    with pytest.raises(RuntimeError, match="AI API 429"):
        LLM(AI_R, httpx.MockTransport(slow), sleep=lambda s: None).chat([{"role": "user", "content": "x"}])
    assert always["i"] == 4, "1 try + 3 retries"


def test_qdrant_a_large_upsert_is_split_into_batches():
    transport, calls = recorder(lambda url, r: (200, {}))
    q = QdrantStore({"url": "http://q", "collection": "docs"}, 3, transport)
    q.upsert([{"id": str(i), "vector": [1, 0, 0], "payload": {}} for i in range(600)])
    puts = [c for c in calls if c["method"] == "PUT"]
    assert [len(c["body"]["points"]) for c in puts] == [256, 256, 88]
    q.upsert([])
    assert len([c for c in calls if c["method"] == "PUT"]) == 3, "an empty upsert sends nothing"


def test_qdrant_transient_failures_are_retried_a_400_is_not():
    n = {"i": 0}

    def flaky(request):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("fetch failed")
        if n["i"] == 2:
            return httpx.Response(408)
        if n["i"] == 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"result": []})

    q = QdrantStore({"url": "http://q", "collection": "docs"}, 3, httpx.MockTransport(flaky), sleep=lambda s: None)
    assert q._req("POST", "/x", {})["result"] == []
    assert n["i"] == 4

    bad = {"i": 0}

    def bad_request(request):
        bad["i"] += 1
        return httpx.Response(400, text="bad request")

    q2 = QdrantStore({"url": "http://q", "collection": "docs"}, 3, httpx.MockTransport(bad_request), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="-> 400"):
        q2._req("POST", "/x", {})
    assert bad["i"] == 1, "a 400 is never retried"

    always = {"i": 0}

    def busy(request):
        always["i"] += 1
        return httpx.Response(503, text="x")

    q3 = QdrantStore({"url": "http://q", "collection": "docs"}, 3, httpx.MockTransport(busy), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="-> 503"):
        q3._req("POST", "/x", {}, retries=2)
    assert always["i"] == 3, "1 try + 2 retries"
