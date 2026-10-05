"""Live test against the REAL cAImanDesk (Vikunja) MCP. Gated: it writes one clearly labelled test task, exercises the full ticket
lifecycle, then DELETES it. Run it yourself:

  CAIMANDESK_LIVE=1 CAIMANDESK_API_TOKEN=tk_... CAIMANDESK_PROJECT_ID=1 pytest tests/integration/test_caimandesk_live.py
"""
import os
import re
import secrets

import pytest

from multisync.config import load_config
from multisync.mcpclient import with_mcp
from multisync.tickets import create_tickets

LIVE = os.environ.get("CAIMANDESK_LIVE") == "1" and os.environ.get("CAIMANDESK_API_TOKEN") and os.environ.get("CAIMANDESK_PROJECT_ID")
pytestmark = pytest.mark.skipif(not LIVE, reason="set CAIMANDESK_LIVE=1, CAIMANDESK_API_TOKEN and CAIMANDESK_PROJECT_ID")


def test_real_caimandesk_mcp_review_ticket_lifecycle_create_find_comment_close_and_cleanup():
    alerts = load_config(os.environ)["alerts"]
    t = create_tickets(alerts)
    assert t.transport == "mcp"
    repo = f"itest/{secrets.token_hex(3)}"
    review = dict(repo=repo, commit="abcdef1234", pr_url="https://github.com/example/docs/pull/0", environment="TEST",
                  items=[{"path": "overview.md", "precision": 1, "recall": 0.9, "style": 1, "quality": 1}])
    headers = {"Authorization": f"Bearer {alerts['deskToken']}"}
    task_id = None
    try:
        first = t.open_review(**review)
        task_id = first["id"]
        assert first["created"] is True
        assert re.search(rf"/tasks/{task_id}$", first["url"])

        again = t.open_review(**review)
        assert again["id"] == task_id, "the open ticket is found again, no duplicate"
        assert again["created"] is False

        def check(call):
            task = call("tasks_read_one", {"id": task_id})
            assert task["title"] == f"[docs-review] {repo} @ abcdef1"
            comments = call("tasks_comments_read_all", {"task_id": task_id})
            assert any(re.search("Pull request updated", c.get("comment", "")) for c in (comments if isinstance(comments, list) else [comments])), "the repeat became a real comment"

        with_mcp(alerts["deskMcpUrl"], check, headers=headers)
        assert t.approve({"id": task_id}, review["pr_url"]) is True
        assert with_mcp(alerts["deskMcpUrl"], lambda call: call("tasks_read_one", {"id": task_id}), headers=headers)["done"] is True
    finally:
        if task_id:
            try:
                with_mcp(alerts["deskMcpUrl"], lambda call: call("tasks_delete", {"id": task_id}), headers=headers)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup failed for task {task_id}: {e}")
