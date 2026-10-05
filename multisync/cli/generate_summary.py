"""GitHub Actions step summary built from pipeline-results.json."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

ICON = {"published": "✅", "pending_review": "👀", "refreshed": "♻️", "skipped": "⏭️", "fallback": "🚫"}


def pct(n) -> str:
    return f"{n:.2f}" if isinstance(n, (int, float)) else "—"


def render(data: dict) -> str:
    results, repo, commit, run_id, trust = data["results"], data["repo"], data["commit"], data["runId"], data["trust"]
    out: list[str] = []
    w = out.append
    w("# Documentation Sync Summary")
    w("")
    w("| Field | Value |\n| :--- | :--- |")
    w(f"| Run | `{run_id}` |")
    w(f"| Source | `{repo}` @ `{str(commit)[:7]}` |")
    w(f"| Trust level | `{trust}` |")
    w("")
    w("| Outcome | Count |\n| :--- | ---: |")
    for o, icon in ICON.items():
        w(f"| {icon} {o} | {sum(1 for r in results if r['outcome'] == o)} |")
    w("")
    w("## Decisions")
    w("")
    w("| File | Outcome | Reviewer action | Tag | Precision | Recall | Why |\n| :--- | :--- | :--- | :--- | ---: | ---: | :--- |")
    for r in results:
        f = (r.get("metrics") or {}).get("final") or {}
        tag = f"`{r['rootCauseTag']}`" if r.get("rootCauseTag") else "—"
        why = str(r.get("reason")).replace("|", "\\|")
        w(f"| `{r['path']}` | {ICON.get(r['outcome'], '')} {r['outcome']} | {r.get('reviewerAction')} | {tag} | {pct(f.get('precision'))} | {pct(f.get('recall'))} | {why} |")

    with_ctx = [r for r in results if r.get("context") and (r["context"].get("code") or r["context"].get("semantic") or r["context"].get("gar"))]
    if with_ctx:
        w("")
        w("## Context the run retrieved")
        for r in with_ctx:
            c = r["context"]
            w("")
            w(f"**`{r['path']}`**")
            if c.get("gar"):
                w(f"- GAR queries ({len(c['gar'])}): " + " | ".join(f'"{str(g)[:70]}..."' for g in c["gar"]))
            if c.get("snapshot"):
                s = c["snapshot"]
                more = ", ..." if s["count"] > 8 else ""
                w(f"- Based on {s['count']} of {s['scoped']} files in scope ({s['chars']} characters): " + ", ".join(f"`{x}`" for x in s["files"][:8]) + more)
            if c.get("facts"):
                w(f"- Facts found in the code: {len(c['facts'])}")
            if c.get("code"):
                w(f"- Code context ({len(c['code'])}): " + ", ".join(f"`{x['path']}:{x['start']}-{x['end']}` ({x['score']})" for x in c["code"][:6]))
            if c.get("semantic"):
                w(f"- Semantic context ({len(c['semantic'])}): " + ", ".join(f"{x['kind']} `{x['path']}` ({x['score']})" for x in c["semantic"][:6]))

    fallbacks = [r for r in results if r["outcome"] == "fallback"]
    if fallbacks:
        w("")
        w("## Needs a human")
        w("")
        for r in fallbacks:
            w(f"- `{r['path']}` — `{r.get('rootCauseTag')}`" + (f" → {r['ticket']}" if r.get("ticket") else ""))
    w("")
    w(f"*Completed {datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}*")
    return "\n".join(out) + "\n"


def main() -> None:
    try:
        with open("pipeline-results.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"could not read pipeline-results.json: {e}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(render(data))


if __name__ == "__main__":
    main()
