"""Minimal MCP client over Streamable HTTP (JSON-RPC 2.0 over POST; the reply is JSON or a short SSE stream).

One short-lived session per batch of calls: initialize, call tools, close. No state is kept between runs.
Vikunja's built-in MCP at /api/v2/mcp speaks this transport.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx

PROTOCOL = "2025-03-26"


def unwrap(result: dict) -> Any:
    """Tool results arrive as text content (JSON) and/or structuredContent; normalise to plain Python."""
    if result.get("isError"):
        msg = " ".join(c.get("text", "") for c in result.get("content") or [] if c.get("text")) or "tool error"
        raise RuntimeError(f"MCP tool error: {msg[:300]}")
    sc = result.get("structuredContent")
    if sc is not None:
        return sc["result"] if "result" in sc and len(sc) == 1 else sc
    parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    parsed = []
    for t in parts:
        try:
            parsed.append(json.loads(t))
        except ValueError:
            parsed.append(t)
    if not parsed:
        return None
    return parsed[0] if len(parsed) == 1 else parsed  # a list result may arrive as one item per element


class _Session:
    def __init__(self, client: httpx.Client, url: str, timeout: float):
        self.client, self.url, self.timeout = client, url, timeout
        self.session_id: str | None = None
        self._id = 0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _message(res: httpx.Response, rid: int) -> dict:
        if "text/event-stream" in res.headers.get("content-type", ""):
            for line in res.text.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == rid:
                        return msg
            raise RuntimeError("MCP: no response message in the event stream")
        msg = res.json()
        if isinstance(msg, list):
            msg = next((m for m in msg if m.get("id") == rid), {})
        return msg

    def request(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        rid = self._id
        res = self.client.post(self.url, headers=self._headers(), content=json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        if res.status_code >= 400:
            raise RuntimeError(f"MCP {method} -> HTTP {res.status_code}: {res.text[:300]}")
        if method == "initialize":
            self.session_id = res.headers.get("mcp-session-id") or self.session_id
        msg = self._message(res, rid)
        if "error" in msg:
            raise RuntimeError(f"MCP {method} error: {str(msg['error'])[:300]}")
        return msg.get("result")

    def notify(self, method: str) -> None:
        self.client.post(self.url, headers=self._headers(), content=json.dumps({"jsonrpc": "2.0", "method": method}))

    def close(self) -> None:
        if self.session_id:
            try:
                self.client.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass


def with_mcp(url: str, fn: Callable[[Callable[..., Any]], Any], headers: dict | None = None, timeout: float = 30.0,
             transport: httpx.BaseTransport | None = None) -> Any:
    """Run `fn(call)` where `call(name, args)` invokes one tool and returns its unwrapped result."""
    with httpx.Client(headers=headers or {}, timeout=timeout, transport=transport) as client:
        session = _Session(client, url, timeout)
        try:
            session.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "ai-multisync-tool", "version": "0.3.0"}})
            session.notify("notifications/initialized")
            return fn(lambda name, args=None: unwrap(session.request("tools/call", {"name": name, "arguments": args or {}})))
        finally:
            session.close()
