"""Fallback / remediation. A fallback decision NEVER publishes: it opens a ticket for the technical-writer queue (draft + failed checks),
pings Slack, and leaves everything unmerged so a human must act before anything ships. Delivery failures are logged, never raised:
the run's decision log in the FactStore remains the source of truth."""
from __future__ import annotations

import base64
import sys

import httpx

from .tickets import create_tickets, fallback_html, f2

ticket_html = fallback_html


def ticket_body(d: dict) -> str:
    attempts = "\n".join(
        f"| {a.get('n')}{' (widened)' if a.get('widened') else ''} | {f2(a.get('precision'))} | {f2(a.get('recall'))} | {f2(a.get('style'))} | {f2(a.get('quality'))} | {a.get('failure') or 'pass'} |"
        for a in d.get("attempts") or [])
    feedback = d.get("feedback")
    parts = [
        f"**Repo:** {d.get('repo')}", f"**File:** {d.get('path')}", f"**Commit:** {d.get('commit')}",
        f"**Reviewer action:** {d.get('reviewerAction')}", f"**Root cause tag:** `{d.get('rootCauseTag')}`",
        f"**Reason:** {d.get('reason')}", "",
        attempts and "| attempt | precision | recall | style | quality | failed check |\n|---|---|---|---|---|---|\n" + attempts,
        feedback and "\n**Judge findings**\n" + "\n".join(f"- {f}" for f in feedback),
        d.get("draft") and f"\n<details><summary>Last draft</summary>\n\n```markdown\n{d['draft'][:20000]}\n```\n</details>",
    ]
    return "\n".join(p for p in parts if p)


def _open_github_issue(d, a, client):
    if not a.get("githubToken") or not a.get("githubRepo"):
        return None
    res = client.post(f"https://api.github.com/repos/{a['githubRepo']}/issues",
                      headers={"Authorization": f"Bearer {a['githubToken']}", "Accept": "application/vnd.github+json"},
                      json={"title": f"[docs-sync] {d.get('rootCauseTag')}: {d.get('repo')} {d.get('path')}", "body": ticket_body(d),
                            "labels": ["docs-sync", "needs-technical-writer"]})
    return res.json().get("html_url") if res.is_success else None


def _open_jira_issue(d, a, client):
    if not a.get("jiraBaseUrl") or not a.get("jiraToken") or not a.get("jiraProject"):
        return None
    auth = base64.b64encode(f"{a.get('jiraEmail')}:{a['jiraToken']}".encode()).decode()
    res = client.post(f"{a['jiraBaseUrl']}/rest/api/2/issue", headers={"Authorization": f"Basic {auth}"},
                      json={"fields": {"project": {"key": a["jiraProject"]}, "issuetype": {"name": "Task"},
                                       "summary": f"[docs-sync] {d.get('rootCauseTag')}: {d.get('repo')} {d.get('path')}",
                                       "description": ticket_body(d), "labels": ["docs-sync"]}})
    return f"{a['jiraBaseUrl']}/browse/{res.json()['key']}" if res.is_success else None


def escalate(d: dict, alerts, transport: httpx.BaseTransport | None = None) -> str | None:
    ticket = None
    provider = alerts.get("ticketProvider") if alerts.get("ticketOnFallback") else None
    with httpx.Client(transport=transport, timeout=30) as client:
        try:
            if provider == "caimandesk":
                ticket = create_tickets(alerts, transport).open_fallback(d)
            if provider == "github":
                ticket = _open_github_issue(d, alerts, client)
            if provider == "jira":
                ticket = _open_jira_issue(d, alerts, client)
        except Exception as e:  # noqa: BLE001
            print(f"ticket creation failed: {e}", file=sys.stderr)
        if alerts.get("slackWebhook"):
            try:
                client.post(alerts["slackWebhook"], json={"text": f":warning: docs-sync *{d.get('rootCauseTag')}* — {d.get('repo')}/{d.get('path')}\n{d.get('reason')}{chr(10) + ticket if ticket else ''}"})
            except Exception as e:  # noqa: BLE001
                print(f"slack alert failed: {e}", file=sys.stderr)
    return ticket
