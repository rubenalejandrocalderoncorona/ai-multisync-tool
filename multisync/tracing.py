"""Optional tracing to Arize Phoenix, over OpenTelemetry with the OpenInference span conventions.

Off unless PHOENIX_COLLECTOR_ENDPOINT is set (for example http://phoenix.multirepo.svc.cluster.local:6006). With it, every run becomes one trace:
  docs-sync <repo> <page>            root span (AGENT): the run, its outcome, tier and cost
    node:<name>                      one span per LangGraph node (CHAIN), with the node's decision data
      llm:<tier> <model>             one span per model call (LLM): messages, answer, tokens, cost
      embeddings                     one span per embedding call (EMBEDDING)
      draft                          the model-written page of one write_draft attempt: input = fact sheet + doc plan, output = the draft, retrieval.documents = the
                                     retrieved chunks (what the nightly evaluator scores, see docs/DRAFT-EVALS.md)
Spans are grouped into a session per run (session.id = run id) and into the Phoenix project PHOENIX_PROJECT_NAME. PHOENIX_API_KEY, when set, is
sent as a bearer token. Nothing here can fail a run: any tracing error is swallowed. When tracing is off, `span()` costs a function call.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re

_tracer = None
_provider = None
MAX_TEXT = 12000


def _clip(value, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"\n...[{len(text) - limit} more characters not sent]"


SOURCE_BUDGET = 30000
_FILE_BLOCK = re.compile(r"(?m)^### FILE: (.+)\n")


def content_capture_enabled(env=None) -> bool:
    """PHOENIX_CAPTURE_CONTENT=0|false|off|no keeps source text off spans (paths and hashes are still recorded). On by default."""
    return str((os.environ if env is None else env).get("PHOENIX_CAPTURE_CONTENT", "1")).strip().lower() not in ("0", "false", "off", "no")


def source_budget(env=None) -> int:
    try:
        return max(0, int((os.environ if env is None else env).get("DRAFT_SOURCE_CHARS", SOURCE_BUDGET)))
    except ValueError:
        return SOURCE_BUDGET


def source_attrs(snapshot: str, changed_files: list | None = None, budget: int | None = None, capture: bool | None = None) -> dict:
    """What a draft was written from, bounded: file paths, a sha256 of the whole snapshot, its size, the changed files, and (only when content capture is on)
    the snapshot text clipped to `budget` characters, changed files first so the part that matters survives the cut."""
    snapshot = snapshot or ""
    budget = source_budget() if budget is None else budget
    capture = content_capture_enabled() if capture is None else capture
    parts = _FILE_BLOCK.split(snapshot)
    blocks = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)] if len(parts) > 1 else [("(source)", snapshot)]
    changed = [str(c) for c in (changed_files or [])]
    out: dict = {"multisync.source.sha256": hashlib.sha256(snapshot.encode("utf-8", "replace")).hexdigest(), "multisync.source.chars": len(snapshot),
                 "multisync.source.files": json.dumps([b[0][:200] for b in blocks][:300]), "multisync.source.changed_files": json.dumps([c[:200] for c in changed[:100]])}
    if not capture or not snapshot or budget <= 0:
        return out
    ordered = sorted(blocks, key=lambda b: 0 if b[0] in changed else 1)
    text, used, cut = [], 0, 0
    for path, body in ordered:
        block = f"### FILE: {path}\n{body}"
        room = budget - used
        if room <= 0:
            cut += len(block)
            continue
        text.append(block if len(block) <= room else block[:room])
        used += len(text[-1])
        cut += len(block) - len(text[-1])
    out["multisync.source.text"] = "\n".join(text) + (f"\n...[{cut} more characters not recorded]" if cut else "")
    return out


def document_attrs(docs: list[dict], limit: int = 12, chars: int = 1500) -> dict:
    """OpenInference retrieval.documents.<i>.document.* attributes for the chunks a step read. Bounded: `limit` chunks, `chars` characters each."""
    out: dict = {}
    for i, d in enumerate(docs[:limit]):
        out[f"retrieval.documents.{i}.document.content"] = _clip(d.get("content") or "", chars)
        if d.get("id") is not None:
            out[f"retrieval.documents.{i}.document.id"] = str(d["id"])
        if isinstance(d.get("score"), (int, float)):
            out[f"retrieval.documents.{i}.document.score"] = float(d["score"])
    return out


def configure(endpoint: str | None = None, exporter=None, project: str | None = None, api_key: str | None = None) -> bool:
    """Set up the exporter. `exporter` is for tests (an in-memory exporter). Returns True when tracing is on."""
    global _tracer, _provider
    endpoint = endpoint or os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    if not endpoint and exporter is None:
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        resource = Resource.create({"openinference.project.name": project or os.environ.get("PHOENIX_PROJECT_NAME", "multirepo-agent-docs"), "service.name": "multisync"})
        provider = TracerProvider(resource=resource)
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            key = api_key or os.environ.get("PHOENIX_API_KEY")
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", headers={"authorization": f"Bearer {key}"} if key else None, timeout=10)))
        _provider, _tracer = provider, provider.get_tracer("multisync")
        return True
    except Exception as e:  # noqa: BLE001 - tracing must never stop a run
        print(f"tracing disabled: {e}")
        _provider = _tracer = None
        return False


def enabled() -> bool:
    return _tracer is not None


class Span:
    """A thin wrapper so callers never touch OpenTelemetry directly. A no-op when tracing is off."""

    def __init__(self, span=None):
        self._span = span

    def set(self, **attrs) -> None:
        if self._span is None:
            return
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                self._span.set_attribute(k.replace("__", "."), v if isinstance(v, (str, bool, int, float)) else _clip(v))
            except Exception:  # noqa: BLE001
                pass

    def io(self, input=None, output=None) -> None:
        if self._span is None:
            return
        if input is not None:
            self.set(**{"input.value": _clip(input)})
        if output is not None:
            self.set(**{"output.value": _clip(output)})

    def error(self, err: BaseException) -> None:
        if self._span is not None:
            try:
                from opentelemetry.trace import Status, StatusCode

                self._span.set_status(Status(StatusCode.ERROR, str(err)[:300]))
                self._span.record_exception(err)
            except Exception:  # noqa: BLE001
                pass


@contextlib.contextmanager
def span(name: str, kind: str = "CHAIN", **attrs):
    """with tracing.span('node:judge', 'CHAIN', **{'session.id': run_id}) as sp: ..."""
    if _tracer is None:
        yield Span(None)
        return
    try:
        cm = _tracer.start_as_current_span(name)
        raw = cm.__enter__()
    except Exception:  # noqa: BLE001
        yield Span(None)
        return
    sp = Span(raw)
    sp.set(**{"openinference.span.kind": kind}, **attrs)
    try:
        yield sp
    except BaseException as e:
        sp.error(e)
        try:
            cm.__exit__(type(e), e, e.__traceback__)
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def llm_attrs(messages: list[dict], model: str, tier: str, temperature) -> dict:
    out: dict = {"llm.model_name": model, "llm.provider": "deepseek" if "deepseek" in model.lower() else "openai", "multisync.tier": tier, "input.value": _clip(messages)}
    if temperature is not None:
        out["llm.invocation_parameters"] = json.dumps({"temperature": temperature})
    for i, m in enumerate(messages):
        out[f"llm.input_messages.{i}.message.role"] = m.get("role", "")
        out[f"llm.input_messages.{i}.message.content"] = _clip(m.get("content", ""), 8000)
    return out


def flush(timeout_ms: int = 8000) -> None:
    """Send what is buffered. Jobs are short-lived, so this must run before the process exits."""
    if _provider is not None:
        try:
            _provider.force_flush(timeout_ms)
        except Exception:  # noqa: BLE001
            pass
