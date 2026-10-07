"""Test doubles shared by the test suite and the offline demo: a scripted LLM, a deterministic embedding, and a fake OpenAI server."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .factstore import MemoryFactStore
from .vectorstore import MemoryVectorStore


def embed_text(text: str, dim: int = 64) -> list[float]:
    """Deterministic bag-of-words embedding: similar text -> high cosine, no network."""
    v = [0.0] * dim
    for w in re.findall(r"[a-z0-9_]+", text.lower()):
        h = 0
        for ch in w:
            h = (h * 31 + ord(ch)) % dim
        v[h] += 1
    return v


GOOD_DRAFT = ("## Overview\n\nThe alert service exposes an alert API on port 8080 and supports three alert channels. Alert and Silence are the public "
              "operations, and the Port setting controls where the service listens.\n\n## Configuration\n\nSet `ALERT_PORT` to change the port. "
              "Use Silence with an alert id to mute a single alert.\n")

CODE_FACTS = {
    "summary": "Alert service; the commit adds silencing and a configurable port.",
    "facts": [
        {"id": "F1", "text": "The service listens on port 8081", "evidence": "src/a.go:5", "kind": "config", "status": "changed"},
        {"id": "F2", "text": "Alerts can be silenced by id", "evidence": "src/a.go:3", "kind": "behavior", "status": "added"},
        {"id": "F3", "text": "no evidence given", "evidence": "", "kind": "other", "status": "added"},
    ],
    "unclear": ["How silences expire"],
}
GAR_FACTS = {"paragraphs": ["The service listens on a configurable port and is configured through environment variables.",
                            "Alerts can be silenced by id through the silence endpoint.", ""]}
PLAN = {
    "audience": "Engineers integrating with the alert service", "purpose": "Explain delivery and configuration",
    "sections": [{"heading": "Overview", "action": "update", "must_cover": ["F1", "F2", "F99"], "notes": ""},
                 {"heading": "Configuration", "action": "add", "must_cover": ["F1"], "notes": ""}],
    "terminology": [{"term": "silence", "use": "silence"}], "out_of_scope": ["billing"], "gaps": ["How silences expire"],
}
PASS_JUDGE = {"claims": [{"text": "port is 8080", "supported": True}], "facts": [{"text": "port 8080", "covered": True}], "style": 0.9, "quality": 0.9, "notes": []}
HALLUCINATION_JUDGE = {"claims": [{"text": "supports gRPC", "supported": False}, {"text": "port is 8080", "supported": True}],
                       "facts": [{"text": "port 8080", "covered": True}], "style": 0.9, "quality": 0.9, "notes": []}
DOC_V1 = "## Overview\n\nAlert API.\n\n- email\n- slack\n"
DOC_V2 = "## Overview\n\nAlert API on port 8080.\n\n- email\n- slack\n- pagerduty\n\n## Configuration\n\nSet `ALERT_PORT`.\n"


def _tier_of(tier=None, fast=False) -> str:
    return "cheap" if tier == "cheap" or fast else "expensive"


class FakeLLM:
    """Scripted LLM. `judges` is a queue of judge results; the last one repeats. Records every call so tests can assert on cost."""

    def __init__(self, judges=None, draft=GOOD_DRAFT, repo_facts=None, patches=None):
        self.judges = judges or []
        self.patches = patches or []  # queue for section-patch replies: a dict/list is returned, a str is a malformed reply (ValueError); empty = always malformed
        self._p = 0
        self.repo_facts = repo_facts or []
        self.draft = draft
        self._j = 0
        self.calls = {"chat": 0, "judge": 0, "embed": 0, "analyze": 0, "plan": 0, "drafts": [], "analyzeInputs": [], "planInputs": [], "log": [], "garFacts": 0, "patch": 0, "patchInputs": []}

    def embed(self, texts):
        self.calls["embed"] += 1
        return [embed_text(t) for t in texts]

    def chat(self, messages, tier=None, fast=False, temperature=...):
        c = self.calls
        c["chat"] += 1
        sys = messages[0]["content"]
        if "ONE short paragraph" in sys:
            kind = "gar"
        elif "single best template" in sys:
            kind = "template"
        elif "Classify the document" in sys:
            kind = "folder"
        elif "improve spelling" in sys:
            kind = "polish"
        else:
            kind = "draft"
        c["log"].append({"kind": kind, "tier": _tier_of(tier, fast)})
        if kind == "gar":
            return "The service exposes an alert API on port 8080."
        if kind == "template":
            return "DEFAULT"
        if kind == "folder":
            return "features"
        if kind == "polish":
            return f"{messages[1]['content']}\n"
        c["drafts"].append(messages[1]["content"])
        return self.draft(messages, {"tier": tier, "fast": fast}, c) if callable(self.draft) else self.draft

    def chat_json(self, messages, tier=None, fast=False, temperature=...):
        c = self.calls
        sys = (messages[0]["content"] if messages else "") or ""
        if "SECTION PATCH MODE" in sys:
            kind = "patch"
        elif "HYPOTHETICAL documentation" in sys:
            kind = "gar-facts"
        elif "code analyst" in sys:
            kind = "analyze"
        elif "documentation planner" in sys:
            kind = "plan"
        elif "extract atomic facts" in sys:
            kind = "repo-facts"
        else:
            kind = "judge"
        c["log"].append({"kind": kind, "tier": _tier_of(tier, fast)})
        if kind == "patch":
            c["patch"] += 1
            c["patchInputs"].append(messages[1]["content"])
            r = self.patches[min(self._p, len(self.patches) - 1)] if self.patches else "not json"
            self._p += 1
            if callable(r):
                r = r(messages)
            if isinstance(r, str):
                raise ValueError(f"Model did not return JSON: {r[:50]}")
            return r
        if kind == "repo-facts":
            return {"facts": self.repo_facts}
        if kind == "gar-facts":
            c["garFacts"] += 1
            return GAR_FACTS
        if kind == "analyze":
            c["analyze"] += 1
            c["analyzeInputs"].append(messages[1]["content"])
            return CODE_FACTS
        if kind == "plan":
            c["plan"] += 1
            c["planInputs"].append(messages[1]["content"])
            return PLAN
        c["judge"] += 1
        r = self.judges[min(self._j, len(self.judges) - 1)]
        self._j += 1
        return r


def fake_llm(judges=None, draft=GOOD_DRAFT, repo_facts=None, patches=None) -> FakeLLM:
    return FakeLLM(judges, draft, repo_facts, patches)


def make_deps(env=None, llm=None, vectors=None, code_vectors=None, facts=None, registry=None, policy=None) -> dict:
    cfg = load_config({"INTERNAL_AI_API_KEY": "test", "MIN_DIFF_LINES": "3", "MAX_ITERATIONS": "2", "CONTEXT_MIN_SCORE": "0", **(env or {})})
    return {
        "cfg": cfg,
        "llm": llm or fake_llm([PASS_JUDGE]),
        "vectors": vectors or MemoryVectorStore(),
        "codeVectors": code_vectors or MemoryVectorStore(),
        "facts": facts or MemoryFactStore(),
        "registry": registry or {},
        "policy": {"trust": "review", "serviceName": "svc", "styleGuide": "", "glossary": {}, **(policy or {})},
        "instructions": "",
        "templateFiles": [],
        "defaultTemplate": "docs/templates/default-template/default-template.md",
        "runId": "test-run",
    }


class FakeOpenAI:
    """A local OpenAI-compatible server with scripted answers for every pipeline stage. Used by CLI and integration tests."""

    def __init__(self, judge_result=PASS_JUDGE):
        self.hits = {"chat": 0, "embed": 0, "judge": 0, "garFacts": 0, "analyze": 0, "plan": 0}
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                if self.path.endswith("/embeddings"):
                    hits["embed"] += 1
                    out = {"data": [{"index": i, "embedding": embed_text(t)} for i, t in enumerate(body["input"])]}
                else:
                    hits["chat"] += 1
                    sys = body["messages"][0]["content"]
                    user = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
                    if "HYPOTHETICAL documentation" in sys:
                        hits["garFacts"] += 1
                        content = json.dumps(GAR_FACTS)
                    elif "You are a code analyst" in sys:
                        hits["analyze"] += 1
                        content = json.dumps(CODE_FACTS)
                    elif "You are a documentation planner" in sys:
                        hits["plan"] += 1
                        content = json.dumps(PLAN)
                    elif "extract atomic facts" in sys:
                        content = json.dumps({"facts": []})
                    elif "You are the JUDGE" in sys:
                        hits["judge"] += 1
                        content = json.dumps(judge_result)
                    elif "ONE short paragraph" in sys:
                        content = "The project exposes an alert API on port 8081."
                    elif "single best template" in sys:
                        content = "DEFAULT"
                    elif "Classify the document" in sys:
                        content = "features"
                    elif "markdown body of the page" in sys:
                        content = ("## Overview\n\nThe project exposes an alert API on port 8081 and supports email, slack and sms channels. Alert and Silence are the "
                                   "public operations of the service.\n\n## Run\n\nRun the binary and set the port. Use Silence with an alert id to mute an alert.\n")
                    else:
                        content = user
                    out = {"choices": [{"message": {"content": content}}]}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeMcp:
    """An in-process MCP server (Streamable HTTP, JSON replies) with Vikunja's native tool names, for the ticket tests."""

    def __init__(self, token: str = "tok"):
        self.tasks: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.log: list[tuple] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload=None, headers=None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_DELETE(self):
                self._send(200, {})

            def do_POST(self):
                if self.path != "/api/v2/mcp":
                    return self._send(404, {})
                if self.headers.get("authorization") != f"Bearer {token}":
                    return self._send(401, {"error": "unauthorized"})
                msg = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                method, rid = msg.get("method"), msg.get("id")
                if rid is None:  # notification
                    return self._send(202)
                if method == "initialize":
                    return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                                                                      "serverInfo": {"name": "vikunja", "version": "v2.5.0"}}}, {"Mcp-Session-Id": "s1"})
                if method == "tools/call":
                    name, a = msg["params"]["name"], msg["params"].get("arguments") or {}
                    out = outer._call(name, a)
                    return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(out)}]}})
                return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v2/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _call(self, name: str, a: dict):
        self.log.append((name, a))
        if name == "tasks_read_all":
            return [t for t in self.tasks.values() if (not a.get("search") or a["search"].lower() in t["title"].lower()) and not (a.get("filter") == "done = false" and t["done"])]
        if name == "tasks_create":
            t = {"id": len(self.tasks) + 100, "done": False, **a}
            self.tasks[t["id"]] = t
            return t
        if name == "tasks_comments_create":
            self.comments.append(a)
            return {"id": len(self.comments), **a}
        if name == "tasks_update":
            tid = a["id"]
            self.tasks[tid] = {**self.tasks.get(tid, {}), **{k: v for k, v in a.items() if k != "id"}}
            return self.tasks[tid]
        return {}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeVikunjaRest:
    """A tiny Vikunja REST API (token 'tok') for the ticket CLI test."""

    def __init__(self):
        self.tasks: dict[int, dict] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                from urllib.parse import urlparse

                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                path = urlparse(self.path).path

                def send(status, payload):
                    data = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                if self.headers.get("authorization") != "Bearer tok":
                    return send(401, {})
                if re.search(r"/projects/\d+/tasks$", path):
                    if self.command == "GET":
                        return send(200, list(outer.tasks.values()))
                    t = {"id": len(outer.tasks) + 1, "done": False, "comments": [], **body}
                    outer.tasks[t["id"]] = t
                    return send(200, t)
                m = re.search(r"/tasks/(\d+)/comments$", path)
                if m:
                    t = outer.tasks.get(int(m.group(1)))
                    if not t:
                        return send(404, {})
                    t["comments"].append(body["comment"])
                    return send(200, {})
                m = re.search(r"/tasks/(\d+)$", path)
                if m:
                    t = outer.tasks.get(int(m.group(1)))
                    if not t:
                        return send(404, {})
                    if self.command == "POST":
                        t.update(body)
                    return send(200, t)
                return send(404, {})

            do_GET = do_POST = do_PUT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
