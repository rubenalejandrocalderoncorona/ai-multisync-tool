import json
import os
import subprocess
import sys

from multisync.testing import FakeVikunjaRest


def run(args, cwd, env):
    p = subprocess.run([sys.executable, "-m", "multisync.cli.review_ticket", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout.strip(), p.stderr


def test_review_ticket_cli_open_marker_approve_closes_reject_leaves_open_missing_marker_and_dead_server_never_fail(tmp_path):
    v = FakeVikunjaRest()
    (tmp_path / "pipeline-results.json").write_text(json.dumps({
        "repo": "cAImanLabs/cAImanLabsCalendarScheduler", "commit": "abcdef1234567",
        "results": [{"path": "overview.md", "outcome": "pending_review", "metrics": {"final": {"precision": 1, "recall": 0.9, "style": 1, "quality": 1}}},
                    {"path": "x.md", "outcome": "skipped"}]}))
    root = os.path.dirname(os.path.dirname(__file__))
    base = {**os.environ, "PYTHONPATH": root, "CAIMANDESK_URL": v.url, "CAIMANDESK_API_TOKEN": "tok", "CAIMANDESK_PROJECT_ID": "7", "CAIMANDESK_TRANSPORT": "rest"}
    try:
        code, out, err = run(["open"], tmp_path, {**base, "PR_URL": "https://github.com/o/docs/pull/9"})
        assert code == 0, err
        o = json.loads(out.splitlines()[-1])
        assert o["marker"] == f"<!-- multisync:ticket=desk:{o['id']} -->"
        assert o["url"] == f"{v.url}/tasks/{o['id']}"
        t = v.tasks[o["id"]]
        assert t["title"] == "[docs-review] cAImanLabs/cAImanLabsCalendarScheduler @ abcdef1"
        assert "overview.md" in t["description"]
        assert "x.md" not in t["description"], "only pages awaiting review are listed"

        code, out, _ = run(["reject"], tmp_path, {**base, "PR_URL": "https://github.com/o/docs/pull/9", "PR_BODY": f"intro\n{o['marker']}"})
        assert json.loads(out)["ok"] is True
        assert t["done"] is False, "closed-unmerged keeps the ticket open"
        assert "Not approved" in t["comments"][-1]

        code, out, _ = run(["qa"], tmp_path, {**base, "PR_URL": "https://github.com/o/docs/pull/9", "QA_URL": "https://example.org/documentation/qa/",
                                              "PR_BODY": f"x\n{o['marker']}\n<!-- multisync:ticket=desk:999 -->"})
        assert code == 0
        assert "Deployed to QA" in t["comments"][-1]
        assert "documentation/qa" in t["comments"][-1]
        assert t["done"] is False

        code, out, _ = run(["approve"], tmp_path, {**base, "PR_URL": "https://github.com/o/docs/pull/9", "PR_BODY": f"intro\n{o['marker']}"})
        assert json.loads(out)["ok"] is True
        assert t["done"] is True
        assert "Approved" in t["comments"][-1]

        code, out, _ = run(["approve"], tmp_path, {**base, "PR_URL": "u", "PR_BODY": "no marker here"})
        assert code == 0
        assert json.loads(out)["skipped"] is True

        code, out, _ = run(["approve"], tmp_path, {**base, "CAIMANDESK_URL": "http://127.0.0.1:9", "PR_URL": "u", "PR_BODY": o["marker"]})
        assert code == 0, "a dead ticket system must not fail the workflow"
        assert json.loads(out)["ok"] is False

        code, out, _ = run(["open"], tmp_path, {**os.environ, "PYTHONPATH": root, "CAIMANDESK_API_TOKEN": "", "CAIMANDESK_PROJECT_ID": "", "CAIMANDESK_MCP_URL": "", "PR_URL": "u"})
        assert code == 0
    finally:
        v.close()
