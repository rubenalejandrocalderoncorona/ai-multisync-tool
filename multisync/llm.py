"""Minimal OpenAI-compatible client (chat + embeddings) on httpx. Works with OpenAI, DeepSeek, vLLM, Ollama (/v1), etc."""
from __future__ import annotations

import copy
import json
import math
import re
import sys
import time
from typing import Any, Callable

import httpx

from . import tracing

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}


def retry_delay_ms(response: httpx.Response | None, body_text: str, attempt: int) -> float:
    """Milliseconds to wait: the server's own hint (Retry-After header or "try again in 25.8s" in the body), else exponential backoff."""
    if response is not None:
        try:
            header = float(response.headers.get("retry-after", ""))
            if header > 0:
                return min(header * 1000, 90_000)
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)\s*(ms|s)\b", str(body_text or ""), re.I)
    if m:
        ms = float(m.group(1)) if m.group(2).lower() == "ms" else float(m.group(1)) * 1000
        return min(ms + 750, 90_000)
    return min(1000 * 2**attempt, 30_000)


def _new_usage() -> dict:
    return {
        "cheap": {"calls": 0, "in": 0, "out": 0, "usd": 0.0},
        "expensive": {"calls": 0, "in": 0, "out": 0, "usd": 0.0},
        "embed": {"calls": 0, "tokens": 0},
    }


class LLM:
    """`ai_config` comes from load_config().ai. `tiers.{cheap,expensive}` pick the endpoint, key, model and temperature per call.
    A flat config (no tiers) behaves as one provider."""

    def __init__(self, ai_config, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = ai_config
        self._client = httpx.Client(transport=transport, timeout=float(ai_config.get("timeoutMs", 120000)) / 1000)
        self.sleep = lambda ms: sleep(ms / 1000)

        def flat(model):
            return {"baseUrl": ai_config["baseUrl"], "chatPath": ai_config["chatPath"], "apiKey": ai_config["apiKey"], "model": model,
                    "temperature": 0, "priceIn": 0, "priceOut": 0}

        self.tiers = ai_config.get("tiers") or {
            "expensive": flat(ai_config["model"]),
            "cheap": flat(ai_config.get("fastModel") or ai_config["model"]),
        }
        # Running totals per tier, so every stage can report what it cost.
        self.usage = _new_usage()

    def snapshot_usage(self) -> dict:
        """Copy of the running totals; subtract two snapshots to get one stage's spend."""
        return copy.deepcopy(self.usage)

    def _post(self, path: str, body: dict, endpoint=None) -> dict:
        """POST with retries on transient failures: rate limits (429), server errors and network errors.
        A stage that loses a whole run to a one-minute rate limit is a reliability bug, not a content failure."""
        endpoint = endpoint if endpoint is not None else self.cfg
        max_retries = int(self.cfg.get("maxRetries", 5))
        attempt = 0
        while True:
            headers = {"Content-Type": "application/json"}
            if endpoint.get("apiKey"):
                headers["Authorization"] = f"Bearer {endpoint['apiKey']}"
            try:
                res = self._client.post(f"{endpoint['baseUrl']}{path}", headers=headers, content=json.dumps(body))
                text = res.text
            except httpx.HTTPError as err:
                # network error / timeout: retry, the request never reached a verdict
                if attempt >= max_retries:
                    raise
                wait = retry_delay_ms(None, "", attempt)
                print(f"llm: {err}; retry {attempt + 1}/{max_retries} in {wait / 1000:.1f}s", file=sys.stderr)
                self.sleep(wait)
                attempt += 1
                continue
            if res.is_success:
                return json.loads(text)
            if res.status_code in RETRYABLE and attempt < max_retries:
                wait = retry_delay_ms(res, text, attempt)
                print(f"llm: HTTP {res.status_code}; retry {attempt + 1}/{max_retries} in {wait / 1000:.1f}s", file=sys.stderr)
                self.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"AI API {res.status_code} ({endpoint.get('model') or 'embeddings'}): {text[:300]}")

    @staticmethod
    def tier_of(opts: dict | None = None) -> str:
        """Which tier a call uses. `fast=True` is the historic spelling of tier "cheap"."""
        opts = opts or {}
        return "cheap" if opts.get("tier") == "cheap" or opts.get("fast") else "expensive"

    def chat(self, messages: list[dict], *, tier: str | None = None, fast: bool = False, temperature: Any = ..., json_mode: bool = False) -> str:
        """Assistant message content. `json_mode` asks the API for a syntactically valid JSON object (response_format json_object): the model can no
        longer break the JSON with an unescaped quote inside a claim. An endpoint that rejects the parameter is remembered and called without it."""
        t = self.tier_of({"tier": tier, "fast": fast})
        ep = self.tiers[t]
        body = {"model": ep["model"], "messages": messages}
        if json_mode and not ep.get("noJsonMode"):
            body["response_format"] = {"type": "json_object"}
        temp = ep.get("temperature") if temperature is ... else temperature
        if temp is not None:  # some models reject any explicit value
            body["temperature"] = temp
        with tracing.span(f"llm:{t} {ep['model']}", "LLM", **tracing.llm_attrs(messages, ep["model"], t, temp)) as sp:
            try:
                data = self._post(ep["chatPath"], body, ep)
            except RuntimeError as err:
                if "response_format" not in body or "response_format" not in str(err):
                    raise
                ep["noJsonMode"] = True  # this endpoint/model does not accept json mode: do not ask again
                body.pop("response_format")
                data = self._post(ep["chatPath"], body, ep)
            u = data.get("usage") or {}
            pin, pout = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
            usd = (pin * (ep.get("priceIn") or 0) + pout * (ep.get("priceOut") or 0)) / 1e6
            slot = self.usage[t]
            slot["calls"] += 1
            slot["in"] += pin
            slot["out"] += pout
            slot["usd"] += usd
            choices = data.get("choices") or [{}]
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
            sp.set(**{"output.value": text[:12000], "llm.output_messages.0.message.role": "assistant", "llm.output_messages.0.message.content": text[:8000],
                      "llm.token_count.prompt": pin, "llm.token_count.completion": pout, "llm.token_count.total": pin + pout, "multisync.cost_usd": round(usd, 6)})
            return text

    def chat_json(self, messages: list[dict], **opts) -> Any:
        return parse_json(self.chat(messages, json_mode=True, **opts))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input, in order. Always the primary (OpenAI) endpoint."""
        if not texts:
            return []
        with tracing.span("embeddings", "EMBEDDING", **{"embedding.model_name": self.cfg["embedModel"], "multisync.inputs": len(texts)}) as sp:
            data = self._post(self.cfg["embedPath"], {"model": self.cfg["embedModel"], "input": texts}, {**self.cfg, "model": None})
            self.usage["embed"]["calls"] += 1
            tokens = (data.get("usage") or {}).get("total_tokens") or 0
            self.usage["embed"]["tokens"] += tokens
            sp.set(**{"llm.token_count.total": tokens})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]

    def close(self) -> None:
        self._client.close()


def repair_json(text: str) -> str:
    """Fix the two ways a model usually breaks JSON: a double quote INSIDE a string value (a claim that quotes a name) and a trailing comma.
    A quote closes a string only when the next non-space character is , : } or ]; any other quote inside a string is escaped."""
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                out.append(ch + text[i + 1])
                i += 2
                continue
            if ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",:}]":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def parse_json(raw: str) -> Any:
    clean = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    clean = re.sub(r"\s*```$", "", clean).strip()
    m = re.search(r"\{[\s\S]*\}", clean)
    candidates = [clean] + ([m.group(0)] if m and m.group(0) != clean else [])
    first_err = None
    for c in candidates:
        for text in (c, repair_json(c)):
            try:
                return json.loads(text)
            except ValueError as e:
                first_err = first_err or e
    if m is None:
        raise ValueError(f"Model did not return JSON: {raw[:200]}") from None
    raise ValueError(f"Model returned invalid JSON ({first_err}): {raw[:200]}") from None


def cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb)) if na and nb else 0.0
