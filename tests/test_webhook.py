import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from multisync.webhook import Config, job_manifest, make_handler, plan, valid_sig

SECRET = "test-" + "x" * 20  # a throwaway fixture, not a credential


def cfg():
    c = Config({"ALLOWED_REPOS": "cAImanLabs/Calendar", "CENTRAL_REPO": "me/docs", "TARGET_BRANCH": "qa"}, secret=SECRET)
    c.namespace = "multirepo"
    c.image = "img"
    return c


def sign(body: bytes, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature():
    body = b'{"a":1}'
    assert valid_sig(SECRET.encode(), body, sign(body))
    for h in ("", None, "sha256=", "sha256=zz", sign(body, "other-secret-value"), sign(body).replace("sha256=", "sha1=")):
        assert not valid_sig(SECRET.encode(), body, h)


PUSH = {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40, "repository": {"full_name": "cAImanLabs/Calendar", "default_branch": "main"},
        "commits": [{"added": ["docs/a.md"], "modified": ["README.md", "../etc/passwd", "x;rm -rf"], "removed": []}]}


def test_plan_push():
    j, why = plan(cfg(), "push", PUSH)
    assert j and j["mode"] == "sync" and j["target"] == "qa" and j["files"] == ["docs/a.md", "README.md"], why
    for label, mutate in {
        "other repo": lambda p: {**p, "repository": {**p["repository"], "full_name": "evil/repo"}},
        "other branch": lambda p: {**p, "ref": "refs/heads/dev"},
        "deleted branch": lambda p: {**p, "after": "0" * 40},
    }.items():
        assert plan(cfg(), "push", mutate(PUSH))[0] is None, label
    assert plan(cfg(), "issues", PUSH)[0] is None


def test_first_push_of_a_branch_is_a_full_sync_an_ordinary_push_is_not():
    j, _ = plan(cfg(), "push", {**PUSH, "before": "0" * 40})
    assert j["full"] is True and j["before"] == ""
    env = {e["name"]: e["value"] for e in job_manifest(cfg(), {**j, "mode": "sync"}, now=1)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["FULL_SYNC"] == "1"
    j2, _ = plan(cfg(), "push", {**PUSH, "before": "a" * 40})
    assert j2["full"] is False
    env2 = {e["name"] for e in job_manifest(cfg(), {**j2, "mode": "sync"}, now=1)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "FULL_SYNC" not in env2


def test_job_deadline_default_is_two_hours_and_configurable_within_bounds():
    m = job_manifest(cfg(), {"mode": "sync", "source_repo": "a/b", "sha": "ABCDEF123456", "before": "", "target": "qa", "files": []}, now=1)
    assert m["spec"]["activeDeadlineSeconds"] == 7200
    assert Config({"JOB_DEADLINE_SECONDS": "99999"}, secret="x" * 20).job_deadline == 14400


def test_plan_dispatch_and_pr():
    j, why = plan(cfg(), "repository_dispatch", {"client_payload": {"repository": "cAImanLabs/Calendar", "sha": "abc1234", "target_branch": "staging", "changed_files": "docs/a.md docs/b.md"}})
    assert j and j["target"] == "staging" and len(j["files"]) == 2, why
    assert plan(cfg(), "repository_dispatch", {"client_payload": {"repository": "cAImanLabs/Calendar", "sha": "abc; id"}})[0] is None
    pr = {"action": "closed", "repository": {"full_name": "me/docs"}, "pull_request": {"merged": True, "merge_commit_sha": "abcdef1", "head": {"ref": "docs-sync/x-abc"}, "base": {"ref": "qa", "sha": "1234567"}}}
    j, why = plan(cfg(), "pull_request", pr)
    assert j and j["mode"] == "index" and j["merge_sha"] == "abcdef1", why
    assert plan(cfg(), "pull_request", {**pr, "pull_request": {**pr["pull_request"], "merged": False}})[0] is None
    assert plan(cfg(), "pull_request", {**pr, "pull_request": {**pr["pull_request"], "head": {"ref": "feature/x"}}})[0] is None


def test_manifest():
    m = job_manifest(cfg(), {"mode": "sync", "source_repo": "a/b", "sha": "ABCDEF123456", "before": "", "target": "qa", "files": []}, now=1700000000)
    s = json.dumps(m)
    assert m["metadata"]["name"] == "multisync-sync-abcdef1-1700000000"
    for want in ('"ttlSecondsAfterFinished": 300', '"backoffLimit": 1', '"automountServiceAccountToken": false'):
        assert want in s


@pytest.fixture()
def server():
    started = []
    c = cfg()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, launcher=lambda cfg_, manifest: (started.append(manifest), manifest["metadata"]["name"])[1]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/sync-webhook", started
    httpd.shutdown()
    httpd.server_close()


def post(url, body: bytes, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_unsigned_and_badly_signed_are_rejected_a_valid_push_starts_one_job_foreign_repo_is_ignored(server):
    url, started = server
    body = json.dumps(PUSH).encode()
    assert post(url, body, {"X-GitHub-Event": "push"})[0] == 401
    assert post(url, body, {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=00"})[0] == 401
    foreign = json.dumps({**PUSH, "repository": {**PUSH["repository"], "full_name": "evil/repo"}}).encode()
    status, text = post(url, foreign, {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(foreign)})
    assert status == 200 and "ignored: repository not allowed" in text and started == []
    status, text = post(url, body, {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(body)})
    assert status == 202 and text.startswith("job multisync-sync-bbbbbbb-")
    assert len(started) == 1
    ping = b"{}"
    assert post(url, ping, {"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(ping)}) == (200, "ignored: pong\n")
    try:
        urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 405


# ── docs-site deploy ─────────────────────────────────────────────────────────
RUN = {"action": "completed", "repository": {"full_name": "me/docs"},
       "workflow_run": {"name": "Documentation site", "conclusion": "success", "event": "push", "head_branch": "qa", "head_sha": "a" * 40}}


def test_plan_deploy_only_for_successful_push_runs_of_the_docs_workflow_on_qa_or_main():
    j, why = plan(cfg(), "workflow_run", RUN)
    assert j and j["mode"] == "deploy" and j["deploy_env"] == "qa" and j["image"] == "ghcr.io/me/docs:qa-" + "a" * 12, why
    j, _ = plan(cfg(), "workflow_run", {**RUN, "workflow_run": {**RUN["workflow_run"], "head_branch": "main"}})
    assert j["deploy_env"] == "prod" and j["image"].endswith(":prod-" + "a" * 12)
    for label, run in {"failed": {"conclusion": "failure"}, "other workflow": {"name": "CI"}, "pr run": {"event": "pull_request"}, "other branch": {"head_branch": "dev"},
                       "short sha": {"head_sha": "abc"}}.items():
        assert plan(cfg(), "workflow_run", {**RUN, "workflow_run": {**RUN["workflow_run"], **run}})[0] is None, label
    assert plan(cfg(), "workflow_run", {**RUN, "repository": {"full_name": "evil/x"}})[0] is None


def test_deploy_manifest_runs_under_the_deployer_service_account_without_secrets():
    j, _ = plan(cfg(), "workflow_run", RUN)
    m = job_manifest(cfg(), j, now=1700000000)
    spec = m["spec"]["template"]["spec"]
    assert m["metadata"]["name"].startswith("multisync-deploy-aaaaaaa-")
    assert spec["serviceAccountName"] == "multisync-docs-deployer"
    assert "envFrom" not in spec["containers"][0], "the deploy Job gets no secrets"
    assert spec["containers"][0]["command"] == ["python", "-m", "multisync.cli.deploy_site"]


# ── review feedback loop: pull_request_review -> revise Job ───────────────────
REVIEW = {"action": "submitted", "repository": {"full_name": "me/docs"},
          "review": {"id": 555, "state": "changes_requested", "user": {"login": "ruben", "type": "User"}, "author_association": "OWNER", "body": "x; rm -rf /"},
          "pull_request": {"number": 9, "state": "open", "html_url": "https://github.com/me/docs/pull/9", "user": {"login": "docs-bot[bot]"},
                           "head": {"ref": "docs-sync/o-proj-abc1234", "sha": "deadbeef1234"}, "base": {"ref": "qa"}}}


def with_(path, v):
    out = json.loads(json.dumps(REVIEW))
    node = out
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = v
    return out


def test_review_changes_requested_on_a_bot_pr_starts_a_revise_job():
    j, why = plan(cfg(), "pull_request_review", REVIEW)
    assert j and j["mode"] == "revise" and j["review_id"] == 555 and j["head_ref"] == "docs-sync/o-proj-abc1234" and j["pr"]["by"] == "ruben", why
    m = job_manifest(cfg(), j, now=1)
    env = {e["name"]: e["value"] for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert m["metadata"]["name"] == "multisync-revise-9-555", "named by PR and review id, so a redelivery cannot start a second Job"
    assert env["JOB_MODE"] == "revise" and env["PR_NUMBER"] == "9" and env["REVIEW_ID"] == "555" and env["REVIEWED_BY"] == "ruben"
    assert env["HEAD_REF"] == "docs-sync/o-proj-abc1234" and env["CENTRAL_REPO"] == "me/docs" and env["TARGET_BRANCH"] == "qa"
    assert env["PR_URL"] == "https://github.com/me/docs/pull/9"
    assert "rm -rf" not in json.dumps(m), "the review text never reaches the Job"
    assert job_manifest(cfg(), j, now=2)["metadata"]["name"] == m["metadata"]["name"]


@pytest.mark.parametrize("label,payload", [
    ("other repo", with_(["repository", "full_name"], "evil/docs")),
    ("approved", with_(["review", "state"], "approved")),
    ("commented", with_(["review", "state"], "commented")),
    ("edited action", {**REVIEW, "action": "edited"}),
    ("closed pr", with_(["pull_request", "state"], "closed")),
    ("not docs-sync", with_(["pull_request", "head"], {"ref": "feature/x", "sha": "deadbeef1234"})),
    ("other base", with_(["pull_request", "base"], {"ref": "main"})),
    ("human-authored pr", with_(["pull_request", "user"], {"login": "ruben"})),
    ("bot reviewer", with_(["review", "user"], {"login": "docs-bot[bot]", "type": "Bot"})),
    ("outsider", with_(["review", "author_association"], "NONE")),
    ("bad review id", with_(["review", "id"], "1; id")),
    ("bad sha", with_(["pull_request", "head"], {"ref": "docs-sync/x-abc1234", "sha": "zz"})),
])
def test_review_events_that_do_not_qualify_are_ignored(label, payload):
    assert plan(cfg(), "pull_request_review", payload)[0] is None, label


def test_a_duplicate_job_creation_is_not_an_error(monkeypatch):
    import urllib.error
    from multisync import webhook

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 409, "conflict", {}, None)

    monkeypatch.setattr(webhook.urllib.request, "urlopen", boom)
    c = cfg()
    c.api_base = "https://k"
    assert webhook.create_job(c, {"metadata": {"name": "multisync-revise-9-555"}}) == "multisync-revise-9-555"


def test_dispatch_can_limit_and_force_pages():
    payload = {"client_payload": {"repository": "cAImanLabs/Calendar", "sha": "abc1234", "only_pages": ["buyer-flow.md", "../x.md", "a b.md", "ok-2.md"], "force_pages": True}}
    j, why = plan(cfg(), "repository_dispatch", payload)
    assert j and j["only_pages"] == ["buyer-flow.md", "ok-2.md"] and j["force_pages"] is True, why
    env = {x["name"]: x["value"] for x in job_manifest(cfg(), j, now=1)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ONLY_PAGES"] == "buyer-flow.md,ok-2.md" and env["FORCE_PAGES"] == "1"
    payload["client_payload"].pop("only_pages")
    j, _ = plan(cfg(), "repository_dispatch", payload)
    assert j["only_pages"] == [] and j["force_pages"] is False
    env = {x["name"]: x["value"] for x in job_manifest(cfg(), j, now=1)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ONLY_PAGES" not in env and "FORCE_PAGES" not in env


@pytest.mark.parametrize("msg", ["Revert x [skip docs-sync]", "tidy [SKIP DOCS]", "wip [no docs]", "fix [skip-docs-sync] now"])
def test_a_skip_marker_in_the_head_commit_message_opts_the_push_out(msg):
    j, why = plan(cfg(), "push", {**PUSH, "head_commit": {"message": msg}})
    assert j is None and "skip" in why


@pytest.mark.parametrize("msg", ["", "docs: skip nothing", "skip docs", "[skip ci]", "Revert the thing"])
def test_without_the_marker_the_push_still_starts_a_sync(msg):
    j, _ = plan(cfg(), "push", {**PUSH, "head_commit": {"message": msg}})
    assert j and j["mode"] == "sync"
