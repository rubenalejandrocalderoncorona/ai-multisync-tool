"""Ticketing port for cAImanDesk (a Vikunja deployment). Three events, two interchangeable transports.

  events      open_fallback(decision)       a run failed; a human must act
              open_review(...)              a docs PR is waiting in QA for review
              approve(ref) / reject(ref)    the PR was merged / closed unmerged
  transports  mcp   Vikunja's built-in MCP server, /api/v2/mcp (tools tasks_create, tasks_read_all, tasks_update, tasks_comments_create)
              rest  the Vikunja REST API with an API token

Both transports behave identically: duplicates are never opened for the same title, and ticket creation never fails or blocks a run
(callers get None and a warning).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .mcpclient import with_mcp

MARKER = re.compile(r"<!--\s*multisync:ticket=([a-z]+):(\d+)\s*-->")


def esc(t: Any) -> str:
    return ("" if t is None else str(t)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def f2(v: Any, missing: str = "-") -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else missing


# ── transports ────────────────────────────────────────────────────────────────
class _RestBackend:
    """REST: PUT /projects/{id}/tasks, POST /tasks/{id}, comments on repeats."""

    name = "rest"

    def __init__(self, a, transport):
        self.headers = {"Authorization": f"Bearer {a['deskToken']}", "Content-Type": "application/json"}
        self.api = f"{a['deskBaseUrl']}/api/v1"
        self.transport = transport

    def run(self, fn):
        with httpx.Client(transport=self.transport, headers=self.headers, timeout=30) as c:
            api = self.api

            class Ops:
                def find_open(self, project_id, title):
                    res = c.get(f"{api}/projects/{project_id}/tasks", params={"s": title, "per_page": 20})
                    tasks = res.json() if res.is_success else []
                    return next((t for t in tasks or [] if t.get("title") == title and not t.get("done")), None)

                def create(self, project_id, title, description, priority):
                    res = c.put(f"{api}/projects/{project_id}/tasks", content=json.dumps({"title": title, "description": description, "priority": priority}))
                    if not res.is_success:
                        raise RuntimeError(f"cAImanDesk {res.status_code}: {res.text[:200]}")
                    return res.json()

                def note(self, task, html):
                    c.put(f"{api}/tasks/{task['id']}/comments", content=json.dumps({"comment": html}))

                def set_done(self, task, done):
                    cur = c.get(f"{api}/tasks/{task['id']}").json()  # v1 task update replaces every column, so read-modify-write
                    c.post(f"{api}/tasks/{task['id']}", content=json.dumps({**cur, "done": done}))

            return fn(Ops())


class _McpBackend:
    """MCP: Vikunja's built-in server (/api/v2/mcp). Real comments and true partial updates, so it behaves exactly like REST."""

    name = "mcp"

    def __init__(self, a, transport):
        self.url = a["deskMcpUrl"]
        self.headers = {"Authorization": f"Bearer {a['deskToken']}"} if a.get("deskToken") else {}
        self.transport = transport

    def run(self, fn):
        def with_call(call):
            class Ops:
                def find_open(self, project_id, title):
                    tasks = call("tasks_read_all", {"project_id": int(project_id), "search": title, "filter": "done = false", "per_page": 50})
                    return next((t for t in tasks if t.get("title") == title and not t.get("done")), None) if isinstance(tasks, list) else None

                def create(self, project_id, title, description, priority):
                    return call("tasks_create", {"project_id": int(project_id), "title": title, "description": description, "priority": priority})

                def note(self, task, html):
                    call("tasks_comments_create", {"task_id": int(task["id"]), "comment": html})

                def set_done(self, task, done):
                    call("tasks_update", {"id": int(task["id"]), "done": done})

            return fn(Ops())

        return with_mcp(self.url, with_call, headers=self.headers, transport=self.transport)


def pick_backend(a, transport=None):
    if not a.get("deskToken") or not a.get("deskProjectId"):
        return None
    return _RestBackend(a, transport) if (a.get("deskTransport") or "mcp") == "rest" else _McpBackend(a, transport)


# ── bodies ────────────────────────────────────────────────────────────────────
def fallback_html(d: dict) -> str:
    rows = "".join(
        f"<tr><td>{x.get('n')}{' (widened)' if x.get('widened') else ''}</td><td>{f2(x.get('precision'))}</td><td>{f2(x.get('recall'))}</td>"
        f"<td>{f2(x.get('style'))}</td><td>{f2(x.get('quality'))}</td><td>{esc(x.get('failure') or 'pass')}</td></tr>"
        for x in d.get("attempts") or [])
    parts = [
        f"<p><strong>Repo:</strong> {esc(d.get('repo'))}<br><strong>File:</strong> {esc(d.get('path'))}<br><strong>Commit:</strong> {esc(d.get('commit'))}"
        f"<br><strong>Reviewer action:</strong> {esc(d.get('reviewerAction'))}<br><strong>Root cause:</strong> <code>{esc(d.get('rootCauseTag'))}</code>"
        f"<br><strong>Reason:</strong> {esc(d.get('reason'))}</p>",
        rows and f"<table><tr><th>attempt</th><th>precision</th><th>recall</th><th>style</th><th>quality</th><th>failed check</th></tr>{rows}</table>",
        d.get("feedback") and "<p><strong>Judge findings</strong></p><ul>" + "".join(f"<li>{esc(f)}</li>" for f in d["feedback"]) + "</ul>",
        d.get("draft") and f"<details><summary>Last draft</summary><pre>{esc(d['draft'][:15000])}</pre></details>",
    ]
    return "".join(p for p in parts if p)


def eval_flag_html(f: dict, threshold) -> str:
    link = f.get("pr")
    parts = [
        f"<p>The nightly draft evaluation scored a draft <strong>{f2(f.get('score'))}</strong> (below {esc(threshold)}): <code>{esc(f.get('label'))}</code>.</p>",
        f"<p><strong>Repo:</strong> {esc(f.get('repo'))}<br><strong>Page:</strong> <code>{esc(f.get('page'))}</code><br><strong>Change unit:</strong> <code>{esc(f.get('change_unit_id'))}</code>"
        f"<br><strong>Attempt:</strong> {esc(f.get('attempt'))}<br><strong>Phoenix span:</strong> <code>{esc(f.get('span_id'))}</code></p>",
        link and f'<p><strong>Link:</strong> <a href="{esc(link)}">{esc(link)}</a></p>',
        f.get("explanation") and f"<p><strong>Judge:</strong> {esc(f['explanation'])}</p>",
        "<p>Check the page against the code before approving it. Detection only: nothing was changed or blocked.</p>",
    ]
    return "".join(p for p in parts if p)


def review_html(repo: str, commit: str, pr_url: str, items: list[dict] | None = None, environment: str = "QA") -> str:
    rows = "".join(
        f"<tr><td><code>{esc(i.get('path'))}</code></td><td>{f2(i.get('precision'))}</td><td>{f2(i.get('recall'))}</td><td>{f2(i.get('style'))}</td><td>{f2(i.get('quality'))}</td></tr>"
        for i in items or [])
    parts = [
        f"<p>Generated documentation from <strong>{esc(repo)}</strong> at <code>{esc(str(commit)[:7])}</code> is waiting for review in <strong>{esc(environment)}</strong>.</p>",
        f'<p><strong>Pull request:</strong> <a href="{esc(pr_url)}">{esc(pr_url)}</a></p>',
        rows and f"<table><tr><th>page</th><th>precision</th><th>recall</th><th>style</th><th>quality</th></tr>{rows}</table>",
        "<p>Approving means merging the pull request. This ticket is closed automatically when that happens; if the pull request is closed without merging, a note is added here and the ticket stays open.</p>",
    ]
    return "".join(p for p in parts if p)


# ── service ───────────────────────────────────────────────────────────────────
class Tickets:
    def __init__(self, alerts, transport=None):
        self.alerts = alerts
        self.backend = pick_backend(alerts, transport)
        self.enabled = self.backend is not None
        self.transport = self.backend.name if self.backend else None

    def _link(self, t) -> str:
        return f"{self.alerts.get('deskPublicUrl') or self.alerts['deskBaseUrl']}/tasks/{t['id']}"

    def _open_or_note(self, title, html, priority, note_html) -> dict:
        """Open a task, or add a note to the open one with the same title. Returns {id, url, created}."""
        def go(b):
            existing = b.find_open(self.alerts["deskProjectId"], title)
            if existing:
                b.note(existing, note_html)
                return {"id": existing["id"], "url": self._link(existing), "created": False}
            t = b.create(self.alerts["deskProjectId"], title, html, priority)
            return {"id": t["id"], "url": self._link(t), "created": True}
        return self.backend.run(go)

    def open_fallback(self, d: dict) -> str | None:
        if not self.backend:
            return None
        title = f"[docs-sync] {d.get('rootCauseTag')}: {d.get('repo')} {d.get('path')}"
        r = self._open_or_note(title, fallback_html(d), 4 if d.get("rootCauseTag") == "pipeline_error" else 3,
                               f"<p>Failed again at commit <code>{esc(d.get('commit'))}</code>: {esc(d.get('reason'))}</p>")
        return r["url"]

    def open_review(self, repo, commit, pr_url, items=None, environment="QA") -> dict | None:
        """{id, url, created, ref}: ref is what the PR body carries to find the ticket later."""
        if not self.backend:
            return None
        title = f"[docs-review] {repo} @ {str(commit)[:7]}"
        r = self._open_or_note(title, review_html(repo, commit, pr_url, items, environment), 2,
                               f'<p>Pull request updated: <a href="{esc(pr_url)}">{esc(pr_url)}</a></p>')
        return {**r, "ref": f"desk:{r['id']}"}

    def open_eval_flag(self, f: dict, threshold) -> str | None:
        """A draft the nightly evaluator scored below the threshold. Returns the ticket URL; a repeat for the same change unit adds a note."""
        if not self.backend:
            return None
        title = f"[docs-eval] {f.get('repo')}@{str(f.get('commit'))[:7]} {f.get('page')}"
        r = self._open_or_note(title, eval_flag_html(f, threshold), 2, f"<p>Scored again: {f2(f.get('score'))} (below {esc(threshold)}).</p>")
        return r["url"]

    def open_info(self, title: str, body_html: str) -> str | None:
        """An INFORMATIONAL ticket (for example a segment graduation alert): nobody must act, nothing was changed. Lowest priority; a repeat of the same
        title adds a note instead of a second ticket. Returns the ticket URL."""
        if not self.backend:
            return None
        r = self._open_or_note(title, body_html, 1, "<p>Informational: raised again.</p>")
        return r["url"]

    def open_audit(self, audit_id: int, repo: str, commit: str, page: str) -> str | None:
        """A second-pass audit request. By design it names neither the original reviewer nor the review outcome, and does not link the pull request, so the
        auditor judges the page against the code rather than against the first reviewer's verdict. Returns the ticket URL."""
        if not self.backend:
            return None
        title = f"[docs-audit] #{audit_id} {repo}@{str(commit)[:7]} {page}"
        html = (f"<p>Independent accuracy check (audit #{audit_id}).</p>"
                f"<p><strong>Page:</strong> <code>{esc(page)}</code> generated from <strong>{esc(repo)}</strong> at commit "
                f'<a href="https://github.com/{esc(repo)}/tree/{esc(commit)}"><code>{esc(str(commit)[:7])}</code></a> (the code to check it against).</p>'
                "<p>Read the published page and confirm each factual statement against the code at that commit. Do not rely on anyone else's review of it. "
                f"You must not be the person who reviewed the page originally.</p>"
                f"<p>Submit the verdict: <code>multisync audit submit {audit_id} --reviewer &lt;your github login&gt; --accurate yes|no --notes \"...\"</code></p>")
        r = self._open_or_note(title, html, 2, "<p>Still waiting for an audit.</p>")
        return r["url"]

    def qa_deployed(self, ref, pr_url=None, site_url=None) -> bool:
        """The docs PR was merged into the QA branch. Approving means merging (the ticket text says so), so the ticket is noted and CLOSED here;
        the later promotion to production only adds a note (see approve)."""
        if not self.backend:
            return False
        site = f' Review it at <a href="{esc(site_url)}">{esc(site_url)}</a>.' if site_url else ""

        def go(b):
            t = {"id": ref["id"]}
            b.note(t, f'<p><strong>Approved and deployed to QA.</strong>{site} Pull request merged: <a href="{esc(pr_url)}">{esc(pr_url)}</a>. This ticket is now closed. '
                      'It goes to production when the promotion pull request is merged.</p>')
            b.set_done(t, True)
            return True
        return self.backend.run(go)

    def approve(self, ref, pr_url=None) -> bool:
        if not self.backend:
            return False

        def go(b):
            t = {"id": ref["id"]}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            b.note(t, f'<p><strong>Deployed to production.</strong> Promotion pull request merged: <a href="{esc(pr_url)}">{esc(pr_url)}</a> ({today}).</p>')
            b.set_done(t, True)  # normally already closed when the review PR was merged into QA; this also closes a ticket that was reopened
            return True
        return self.backend.run(go)

    def reject(self, ref, pr_url=None) -> bool:
        if not self.backend:
            return False
        return self.backend.run(lambda b: (b.note({"id": ref["id"]}, f'<p><strong>Not approved.</strong> The pull request was closed without merging: <a href="{esc(pr_url)}">{esc(pr_url)}</a>. Regenerate or discard.</p>'), True)[1])


def create_tickets(alerts, transport=None) -> Tickets:
    return Tickets(alerts, transport)


def ticket_ref_from_body(body) -> dict | None:
    """Find the ticket reference a review PR carries in its body."""
    m = MARKER.search(str(body or ""))
    return {"provider": m.group(1), "id": int(m.group(2))} if m else None


def ticket_refs_from_body(body) -> list[dict]:
    """All distinct ticket references in a body: a promotion PR carries one marker per page batch it ships."""
    seen, out = set(), []
    for m in MARKER.finditer(str(body or "")):
        key = f"{m.group(1)}:{m.group(2)}"
        if key not in seen:
            seen.add(key)
            out.append({"provider": m.group(1), "id": int(m.group(2))})
    return out


def ticket_marker(ref: dict) -> str:
    return f"<!-- multisync:ticket={ref.get('ref') or 'desk:' + str(ref['id'])} -->"
