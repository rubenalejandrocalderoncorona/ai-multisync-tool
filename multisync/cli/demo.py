"""End-to-end demo of the agentic workflow. Five scenarios run through the real LangGraph pipeline:

  1 first-publish      new doc              -> prefilter > cross_repo > route > similarity > gar > write_draft > verify_draft > judge > publish
  2 near-duplicate     one number changed   -> prefilter > cross_repo > route > similarity  (cosine short-circuit, NO draft)
  3 structural-change  new section + items  -> full path again (shape change overrides similarity)
  4 cross-repo-block   registered feature   -> fallback + cAImanDesk ticket, zero LLM spend
  5 judge-fallback     impossible precision -> judge loop > widen > fallback + cAImanDesk ticket

Live (real LLM, Qdrant, Postgres, cAImanDesk):   python -m multisync.cli.demo
Offline rehearsal (scripted LLM, in-memory):     python -m multisync.cli.demo --offline

Live needs the env vars from .env.example. Tickets need CAIMANDESK_API_TOKEN + CAIMANDESK_PROJECT_ID.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys

from .. import writer as W
from ..config import load_config
from ..factstore import MemoryFactStore
from ..fallback import escalate
from ..pipeline import process_change
from ..testing import HALLUCINATION_JUDGE, PASS_JUDGE, fake_llm
from ..vectorstore import MemoryVectorStore
from ..wiring import apply_decision, build_deps

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = "demo/alerts-service"

V1 = """# Alerts service

The alerts service delivers notifications when a monitored metric crosses a threshold.

## Delivery channels

- email
- slack

## Configuration

The service listens on port 8080. Set `ALERT_PORT` to change it.
"""
V2 = V1.replace("port 8080", "port 8081")
V3 = f"""{V2}
- pagerduty

## Escalation

An unacknowledged alert escalates to the on-call engineer after 10 minutes.
Set `ESCALATION_MINUTES` to change the delay.
"""
V4 = f"""{V3}
## Channel registry

Channels are declared in `alert_channels_enum`.
The frontend renders every declared channel in its settings page.
New channels appear in the UI without a frontend release.
"""

SCENARIOS = [
    {"name": "first-publish", "before": None, "after": V1, "expect": "published"},
    {"name": "near-duplicate", "before": V1, "after": V2, "expect": "refreshed", "env": {"MIN_DIFF_LINES": "2"}},
    {"name": "structural-change", "before": V2, "after": V3, "expect": "published"},
    {"name": "cross-repo-block", "before": V3, "after": V4, "expect": "fallback"},
    {"name": "judge-fallback", "before": V1, "after": V3.replace("10 minutes", "15 minutes"), "expect": "fallback", "env": {"PRECISION_MIN": "1.01", "MAX_ITERATIONS": "2"}},
]

dim = lambda s: f"\x1b[2m{s}\x1b[0m"  # noqa: E731
bold = lambda s: f"\x1b[1m{s}\x1b[0m"  # noqa: E731
green = lambda s: f"\x1b[32m{s}\x1b[0m"  # noqa: E731
red = lambda s: f"\x1b[31m{s}\x1b[0m"  # noqa: E731
yellow = lambda s: f"\x1b[33m{s}\x1b[0m"  # noqa: E731


def offline_llm(scenario: dict):
    llm = fake_llm([HALLUCINATION_JUDGE if scenario["name"] == "judge-fallback" else PASS_JUDGE])
    # Like a faithful writer: the draft is the source text, so re-indexed chunks resemble later edits.
    import re

    def faithful(messages, opts, calls):
        m = re.search(r"SOURCE \([^)]*\):\n([\s\S]*?)(?:\n\nA reviewer rejected|$)", messages[1]["content"])
        return m.group(1) if m else ""

    llm.draft = faithful
    return llm


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    out = os.path.join(ROOT, "demo-output")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    instructions = open(os.path.join(ROOT, ".github/instructions/DocumentationInstructions.instructions.md"), encoding="utf-8").read()
    templates = os.path.join(ROOT, "docs/templates")
    base = {"vectors": MemoryVectorStore(), "facts": MemoryFactStore()} if offline else dict(build_deps())
    if not offline:
        base["facts"].migrate()
        base["vectors"].ensure_collection()
        base["vectors"].delete_by_path(REPO, "docs/alerts.md")  # start every demo from a clean slate for this demo repo only
    os.chdir(out)

    registry = {"alert_channels_enum": {"owner": REPO, "requires": ["demo/frontend"]}}
    policy = {"trust": "auto", "serviceName": "alerts-service", "styleGuide": "Concise reference style.", "glossary": {}}
    run_id = f"demo-{secrets.token_hex(3)}"
    commits = [x.ljust(40, "0") for x in ("a1b2c3d", "b2c3d4e", "c3d4e5f", "d4e5f6a", "e5f6a7b")]
    summary = []
    mode = yellow("[offline: scripted LLM, in-memory stores]") if offline else green("[live]")
    print(bold(f"\nai-multisync-tool demo  {mode}  run {run_id}\n"))

    for i, sc in enumerate(SCENARIOS):
        cfg = load_config({**os.environ, "INTERNAL_AI_API_KEY": os.environ.get("INTERNAL_AI_API_KEY") or "offline",
                           "SIMILARITY_HIGH": os.environ.get("SIMILARITY_HIGH") or "0.85", **(sc.get("env") or {})})
        llm = offline_llm(sc) if offline else base["llm"]

        class Logger:
            def log(self, e):
                tone = yellow if e["status"] in ("stop", "fallback") else red if e["status"] == "error" else green
                detail = f"{e['ms']:>5}ms  " + json.dumps(e.get("note"), default=str)
                print(f"   {tone(e['status'].ljust(8))} {e['node'].ljust(12)} {dim(detail)}")
                try:
                    base["facts"].record_node_log(e)
                except Exception:  # noqa: BLE001 - demo continues without a persisted log
                    pass

        def escalate_fn(d, cfg=cfg):
            if offline and not cfg["alerts"]["deskToken"]:
                return "(offline) cAImanDesk ticket would be created here"
            return escalate(d, cfg["alerts"])

        deps = {"cfg": cfg, "llm": llm, "vectors": base["vectors"], "facts": base["facts"], "registry": registry, "policy": policy, "instructions": instructions,
                "templateFiles": W.find_template_files(templates), "defaultTemplate": os.path.join(templates, "default-template", "default-template.md"),
                "runId": run_id, "githubHost": "github.com", "logger": Logger(), "escalate": escalate_fn}
        print(bold(f"{i + 1}. {sc['name']}") + dim(f"   expect: {sc['expect']}"))
        change = {"repo": REPO, "filePath": "docs/alerts.md", "before": sc["before"], "after": sc["after"], "commit": commits[i]}
        decision = process_change(change, deps)
        apply_decision(decision, {**base, "llm": llm}, REPO, change["filePath"], change["commit"])
        base["facts"].record_decision(decision)

        ok = decision["outcome"] == sc["expect"]
        print(f"   {green('✔') if ok else red('✘')} {bold(decision['outcome'])}  {decision['reason']}")
        if decision.get("rootCauseTag"):
            print(f"   {dim('root cause:')} {decision['rootCauseTag']}   {dim('reviewer action:')} {decision['reviewerAction']}")
        if decision.get("ticket"):
            print(f"   {dim('ticket:')} {decision['ticket']}")
        print()
        summary.append({"scenario": sc["name"], "expected": sc["expect"], "outcome": decision["outcome"], "ok": ok, "tag": decision.get("rootCauseTag"),
                        "ticket": decision.get("ticket"), "stages": [t["node"] for t in decision["trail"]], "ms": sum(t["ms"] for t in decision["trail"])})

    with open(os.path.join(out, "demo-run.json"), "w", encoding="utf-8") as fh:
        json.dump({"runId": run_id, "offline": offline, "summary": summary}, fh, indent=2)
    passed = sum(1 for s in summary if s["ok"])
    print(bold(f"{passed}/{len(summary)} scenarios behaved as expected."))
    print(dim("Published page: demo-output/src/content/docs/services/alerts-service/   Decision log: demo-output/demo-run.json"))
    if hasattr(base["facts"], "close"):
        base["facts"].close()
    return 0 if passed == len(summary) else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
