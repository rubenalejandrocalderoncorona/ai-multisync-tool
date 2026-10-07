"""Webhook receiver: verifies GitHub's HMAC signature and launches one ephemeral Kubernetes Job per accepted event.

Standard library only (no httpx, no langgraph), so it idles at roughly 15 MB. It runs from the same image as the pipeline Jobs:

  python -m multisync.webhook

Events (all signed with WEBHOOK_SECRET, X-Hub-Signature-256):
  push                 a source repo in ALLOWED_REPOS, on its default branch      -> Job mode "sync"
  repository_dispatch  a source repo in ALLOWED_REPOS (client_payload fields)     -> Job mode "sync"
  pull_request closed  CENTRAL_REPO, merged, head docs-sync/*, base TARGET_BRANCH -> Job mode "index" (which also logs the review outcome)
  pull_request closed  the same, but closed WITHOUT merging                        -> Job mode "review" (logs the review outcome, nothing else)
  pull_request_review  CENTRAL_REPO, submitted "changes_requested" by a human on an open bot-authored docs-sync PR into TARGET_BRANCH
                                                                                  -> Job mode "revise" (redrafts the pages with the review as feedback; one Job per review id)
  workflow_run         CENTRAL_REPO "Documentation site" finished OK on qa/main   -> Job mode "deploy" (rolls the docs site out)
  ping                 -> pong
Anything unsigned gets 401; a signed event that does not qualify gets 200 "ignored: <why>". Payload fields are validated against strict
patterns and reach the Job only as environment variables.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
MAX_BODY = 10 << 20
MAX_FILES = 200
MAX_FILES_SIZE = 16 << 10

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
PAGE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}\.md$")  # a declared page path, e.g. buyer-flow.md
FILE_RE = re.compile(r"^[A-Za-z0-9_@+=,. /-]+$")
ZERO_RE = re.compile(r"^0+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

log = logging.getLogger("multisync.webhook")


class Config:
    def __init__(self, env=None, secret: str | None = None):
        env = os.environ if env is None else env
        self.secret = (secret if secret is not None else env.get("WEBHOOK_SECRET", "")).encode()
        self.allowed = {r.strip().lower() for r in env.get("ALLOWED_REPOS", "").split(",") if REPO_RE.match(r.strip())}
        self.central = env.get("CENTRAL_REPO", "rubenalejandrocalderoncorona/multirepo-agent-docs")
        self.target_branch = env.get("TARGET_BRANCH", "qa")
        self.image = env.get("JOB_IMAGE", "multisync-pipeline:local")
        self.pull_policy = env.get("JOB_IMAGE_PULL_POLICY", "IfNotPresent")
        self.configmap = env.get("JOB_CONFIGMAP", "multisync-config")
        self.secret_name = env.get("JOB_SECRET", "multisync-secrets")
        self.qdrant_url = env.get("QDRANT_URL", "http://qdrant:6333")
        self.namespace = env.get("NAMESPACE", "")
        # A first sync of a repository with many pages runs them one after the other (minutes each, up to six attempts per page).
        self.job_deadline = min(max(int(env.get("JOB_DEADLINE_SECONDS", "7200") or 7200), 600), 14400)
        self.deploy_workflow = env.get("DEPLOY_WORKFLOW", "Documentation site")
        self.deploy_sa = env.get("DEPLOY_SERVICE_ACCOUNT", "multisync-docs-deployer")
        self.prod_branch = env.get("PROD_BRANCH", "main")
        self.api_base = f"https://{env.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:{env.get('KUBERNETES_SERVICE_PORT', '443')}"
        self.token = ""
        self.ssl_ctx: ssl.SSLContext | None = None

    def validate(self) -> None:
        if len(self.secret) < 16:
            raise SystemExit("WEBHOOK_SECRET must be set (at least 16 characters)")
        if not self.allowed:
            raise SystemExit("ALLOWED_REPOS must list at least one org/repo")

    def load_cluster(self) -> None:
        """Namespace, token and CA from the pod's service account."""
        try:
            self.namespace = self.namespace or open(f"{SA_DIR}/namespace").read().strip()
            self.token = open(f"{SA_DIR}/token").read().strip()
            self.ssl_ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
        except OSError as e:
            raise SystemExit(f"not running in a cluster: {e}") from e


def valid_sig(secret: bytes, body: bytes, header: str | None) -> bool:
    prefix = "sha256="
    if not header or not header.startswith(prefix):
        return False
    try:
        got = bytes.fromhex(header[len(prefix):])
    except ValueError:
        return False
    return hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).digest(), got)


def clean_files(items) -> list[str]:
    seen, out, size = set(), [], 0
    for f in items:
        f = (f or "").strip()
        if not f or f in seen or ".." in f or f.startswith("/") or not FILE_RE.match(f):
            continue
        if len(out) >= MAX_FILES or size + len(f) > MAX_FILES_SIZE:
            break
        seen.add(f)
        size += len(f) + 1
        out.append(f)
    return out


def plan(cfg: Config, event: str, p: dict) -> tuple[dict | None, str]:
    """Turn a verified event into a job spec, or explain why it is ignored (never an error for the sender)."""
    if event == "ping":
        return None, "pong"
    repository = p.get("repository") or {}
    full_name = repository.get("full_name", "")
    if event == "push":
        if full_name.lower() not in cfg.allowed:
            return None, "repository not allowed"
        after = p.get("after", "")
        if p.get("deleted") or ZERO_RE.match(after) or not SHA_RE.match(after):
            return None, "branch deleted or invalid sha"
        if p.get("ref") != f"refs/heads/{repository.get('default_branch')}":
            return None, "not the default branch"
        files = [f for c in p.get("commits") or [] for f in [*(c.get("added") or []), *(c.get("modified") or [])]]
        before = p.get("before", "")
        first = not SHA_RE.match(before) or bool(ZERO_RE.match(before))  # a push that creates the branch: nothing to diff against
        if first:
            before = ""
        # `full`: the first push of a repository documents everything in scope (otherwise the run would diff against HEAD~1 and see one commit)
        return {"mode": "sync", "source_repo": full_name, "sha": after, "before": before, "target": cfg.target_branch, "files": clean_files(files), "full": first}, ""
    if event == "repository_dispatch":
        cp = p.get("client_payload") or {}
        repo = cp.get("repository") or full_name
        if repo.lower() not in cfg.allowed or not REPO_RE.match(repo):
            return None, "repository not allowed"
        sha = cp.get("sha", "")
        if not REF_RE.match(sha):
            return None, "invalid sha"
        target = cp.get("target_branch") or cfg.target_branch
        if not REF_RE.match(target):
            return None, "invalid target branch"
        before = cp.get("before", "")
        if not SHA_RE.match(before):
            before = ""
        # only_pages: draft just these declared pages (the context is still loaded for all of them); force_pages: draft them even if their files did not change
        raw = cp.get("only_pages") or []
        only = [x.strip() for x in (raw.split(",") if isinstance(raw, str) else raw) if isinstance(x, str) and PAGE_RE.match(x.strip()) and ".." not in x]
        return {"mode": "sync", "source_repo": repo, "sha": sha, "before": before, "target": target, "files": clean_files((cp.get("changed_files") or "").split()),
                "only_pages": only[:20], "force_pages": bool(cp.get("force_pages")) and bool(only)}, ""
    if event == "pull_request":
        if full_name.lower() != cfg.central.lower():
            return None, "not the central repository"
        pr = p.get("pull_request") or {}
        head_ref = (pr.get("head") or {}).get("ref", "")
        base = pr.get("base") or {}
        if p.get("action") != "closed" or not head_ref.startswith("docs-sync/") or base.get("ref") != cfg.target_branch:
            return None, f"not a closed docs-sync PR into {cfg.target_branch}"
        number = pr.get("number")
        review = {
            "number": number if isinstance(number, int) and number > 0 else 0,
            "url": pr.get("html_url", "") if re.match(rf"^https://github\.com/{re.escape(cfg.central)}/pull/\d+$", pr.get("html_url", ""), re.I) else "",
            "merged": bool(pr.get("merged")),
            "by": (p.get("sender") or {}).get("login", "") if LOGIN_RE.match((p.get("sender") or {}).get("login", "")) else "",
            "at": (pr.get("merged_at") or pr.get("closed_at") or "") if TIME_RE.match(pr.get("merged_at") or pr.get("closed_at") or "") else "",
        }
        if not pr.get("merged"):
            # Closed without merging: nothing is indexed and nothing is routed. The only thing started is the job that logs the outcome.
            head_sha = (pr.get("head") or {}).get("sha", "")
            if not review["number"] or not SHA_RE.match(head_sha or ""):
                return None, "invalid PR number or sha"
            return {"mode": "review", "source_repo": full_name, "sha": head_sha, "before": "", "target": base.get("ref"), "files": [], "pr": review}, ""
        merge, base_sha = pr.get("merge_commit_sha", ""), base.get("sha", "")
        if not SHA_RE.match(merge or "") or not SHA_RE.match(base_sha or ""):
            return None, "invalid shas"
        return {"mode": "index", "source_repo": full_name, "sha": merge, "before": "", "target": base.get("ref"), "files": [], "base_sha": base_sha, "merge_sha": merge, "pr": review}, ""
    if event == "pull_request_review":
        return _plan_review(cfg, p, full_name)
    if event == "workflow_run":
        if full_name.lower() != cfg.central.lower():
            return None, "not the central repository"
        run = p.get("workflow_run") or {}
        branch, sha = run.get("head_branch", ""), run.get("head_sha", "")
        if p.get("action") != "completed" or run.get("name") != cfg.deploy_workflow or run.get("conclusion") != "success" or run.get("event") != "push":
            return None, "not a successful push run of the docs site workflow"
        if branch == cfg.prod_branch:
            env_name = "prod"
        elif branch == cfg.target_branch:
            env_name = "qa"
        else:
            return None, f"branch {branch} is not deployed"
        if not SHA_RE.match(sha) or len(sha) < 12:
            return None, "invalid sha"
        return {"mode": "deploy", "source_repo": full_name, "sha": sha, "before": "", "target": branch, "files": [], "deploy_env": env_name,
                "image": f"ghcr.io/{full_name.lower()}:{env_name}-{sha[:12]}"}, ""
    return None, "event not handled"


REVIEWER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _plan_review(cfg: Config, p: dict, full_name: str) -> tuple[dict | None, str]:
    """A reviewer asked for changes on a docs-sync PR that the bot opened: start one `revise` Job. Every value is validated; nothing from the
    review text (body, comments) is read here, the Job fetches it from the GitHub API."""
    if full_name.lower() != cfg.central.lower():
        return None, "not the central repository"
    review, pr = p.get("review") or {}, p.get("pull_request") or {}
    if p.get("action") != "submitted" or str(review.get("state", "")).lower() != "changes_requested":
        return None, "not a submitted changes_requested review"
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if pr.get("state") != "open" or pr.get("merged"):
        return None, "pull request is not open"
    if not str(head.get("ref", "")).startswith("docs-sync/") or base.get("ref") != cfg.target_branch or not REF_RE.match(head.get("ref", "")):
        return None, f"not an open docs-sync PR into {cfg.target_branch}"
    author = (pr.get("user") or {}).get("login", "")
    if not author.endswith("[bot]") or not LOGIN_RE.match(author):
        return None, "the pull request was not opened by the bot"
    reviewer = review.get("user") or {}
    login = reviewer.get("login", "")
    if login.endswith("[bot]") or reviewer.get("type") == "Bot" or not LOGIN_RE.match(login):
        return None, "reviewer is a bot or has an invalid login"
    if review.get("author_association") not in REVIEWER_ASSOCIATIONS:
        return None, "reviewer is not an owner, member or collaborator"
    number, review_id, head_sha = pr.get("number"), review.get("id"), head.get("sha", "")
    if not (isinstance(number, int) and number > 0 and isinstance(review_id, int) and review_id > 0) or not SHA_RE.match(head_sha or ""):
        return None, "invalid PR number, review id or sha"
    url = pr.get("html_url", "")
    pr_info = {"number": number, "url": url if re.match(rf"^https://github\.com/{re.escape(cfg.central)}/pull/\d+$", url, re.I) else "", "merged": False, "by": login, "at": ""}
    return {"mode": "revise", "source_repo": full_name, "sha": head_sha, "before": "", "target": base["ref"], "files": [], "pr": pr_info,
            "review_id": review_id, "head_ref": head["ref"]}, ""


def job_manifest(cfg: Config, j: dict, now: float | None = None) -> dict:
    short = re.sub(r"[^a-z0-9]", "", j["sha"].lower())[:7]
    prefix = {"index": "multisync-index-", "review": "multisync-review-", "deploy": "multisync-deploy-", "revise": "multisync-revise-"}.get(j["mode"], "multisync-sync-")
    name = f"{prefix}{short}-{int(now if now is not None else time.time())}"
    if j["mode"] == "revise":
        # No timestamp: GitHub redelivering the same review produces the same name, Kubernetes answers 409 and no second Job starts.
        name = f"{prefix}{j['pr']['number']}-{j['review_id']}"

    def e(k, v):
        return {"name": k, "value": v}

    env = [e("JOB_MODE", j["mode"]), e("SOURCE_REPO", j["source_repo"]), e("SOURCE_SHA", j["sha"]), e("SOURCE_BEFORE", j["before"]), e("TARGET_BRANCH", j["target"]),
           e("CHANGED_FILES", "\n".join(j["files"])), e("CENTRAL_REPO", cfg.central), e("BASE_SHA", j.get("base_sha", "")), e("MERGE_SHA", j.get("merge_sha", "")),
           e("QDRANT_URL", cfg.qdrant_url), e("HOME", "/work")]
    if j.get("full"):
        env.append(e("FULL_SYNC", "1"))
    if j.get("only_pages"):
        env.append(e("ONLY_PAGES", ",".join(j["only_pages"])))
        if j.get("force_pages"):
            env.append(e("FORCE_PAGES", "1"))
    pr = j.get("pr")
    if pr:  # review-outcome logging (modes index and review)
        env += [e("PR_NUMBER", str(pr["number"] or "")), e("PR_URL", pr["url"]), e("PR_MERGED", "true" if pr["merged"] else "false"), e("REVIEWED_BY", pr["by"]), e("REVIEWED_AT", pr["at"])]
    if j["mode"] == "revise":
        env += [e("REVIEW_ID", str(j["review_id"])), e("HEAD_REF", j["head_ref"])]
    if j["mode"] == "deploy":
        return _deploy_manifest(cfg, name, j)
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": cfg.namespace, "labels": {"app": "multisync-job", "multisync/mode": j["mode"]}},
        "spec": {
            "ttlSecondsAfterFinished": 300, "backoffLimit": 1, "activeDeadlineSeconds": cfg.job_deadline,
            "template": {
                "metadata": {"labels": {"app": "multisync-job"}},
                "spec": {
                    "restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                    "containers": [{
                        "name": "pipeline", "image": cfg.image, "imagePullPolicy": cfg.pull_policy,
                        "command": ["/app/scripts/job-entrypoint.sh"], "workingDir": "/work",
                        "envFrom": [{"configMapRef": {"name": cfg.configmap, "optional": True}}, {"secretRef": {"name": cfg.secret_name}}],
                        "env": env,
                        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "2Gi"}}],
                },
            },
        },
    }


def _deploy_manifest(cfg: Config, name: str, j: dict) -> dict:
    """A small Job under a service account that can only patch the two docs Deployments. It has no model keys and no tokens."""
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": cfg.namespace, "labels": {"app": "multisync-job", "multisync/mode": "deploy"}},
        "spec": {
            "ttlSecondsAfterFinished": 300, "backoffLimit": 0, "activeDeadlineSeconds": 600,
            "template": {
                "metadata": {"labels": {"app": "multisync-job"}},
                "spec": {
                    "restartPolicy": "Never", "serviceAccountName": cfg.deploy_sa,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000},
                    "containers": [{
                        "name": "deploy", "image": cfg.image, "imagePullPolicy": cfg.pull_policy,
                        "command": ["python", "-m", "multisync.cli.deploy_site"],
                        "env": [{"name": "DEPLOY_ENV", "value": j["deploy_env"]}, {"name": "IMAGE", "value": j["image"]}, {"name": "NAMESPACE", "value": cfg.namespace}],
                        "resources": {"requests": {"cpu": "20m", "memory": "48Mi"}, "limits": {"memory": "128Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True},
                    }],
                },
            },
        },
    }


def create_job(cfg: Config, manifest: dict) -> str:
    req = urllib.request.Request(f"{cfg.api_base}/apis/batch/v1/namespaces/{cfg.namespace}/jobs", data=json.dumps(manifest).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=15) as res:
            if res.status != 201:
                raise RuntimeError(f"kubernetes API {res.status}")
    except urllib.error.HTTPError as e:
        if e.code == 409:  # same Job name: a redelivery of an event that already started its Job (revise Jobs are named by review id)
            log.info("job %s already exists", manifest["metadata"]["name"])
            return manifest["metadata"]["name"]
        raise RuntimeError(f"kubernetes API {e.code}: {e.read()[:300].decode(errors='replace')}") from e
    return manifest["metadata"]["name"]


def make_handler(cfg: Config, launcher=create_job):
    class Handler(BaseHTTPRequestHandler):
        server_version = "multisync-webhook"

        def log_message(self, fmt, *args):  # route access logs through logging
            log.debug(fmt, *args)

        def _reply(self, status: int, text: str) -> None:
            data = (text if text.endswith("\n") else text + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self._reply(200, "ok")
            self._reply(405 if self.path == "/api/sync-webhook" else 404, "method not allowed" if self.path == "/api/sync-webhook" else "not found")

        def do_POST(self):
            if self.path != "/api/sync-webhook":
                return self._reply(404, "not found")
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                return self._reply(413, "body too large")
            body = self.rfile.read(length)
            if not valid_sig(cfg.secret, body, self.headers.get("X-Hub-Signature-256")):
                log.warning("rejected: bad signature from %s", self.headers.get("X-Forwarded-For", self.client_address[0]))
                return self._reply(401, "invalid signature")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._reply(400, "invalid json")
            event, delivery = self.headers.get("X-GitHub-Event", ""), self.headers.get("X-GitHub-Delivery", "")
            spec, why = plan(cfg, event, payload)
            if spec is None:
                log.info("ignored event=%s delivery=%s: %s", event, delivery, why)
                return self._reply(200, f"ignored: {why}")
            try:
                name = launcher(cfg, job_manifest(cfg, spec))
            except Exception as e:  # noqa: BLE001
                log.error("job creation failed event=%s delivery=%s: %s", event, delivery, e)
                return self._reply(502, "could not start job")
            log.info("started %s event=%s delivery=%s mode=%s repo=%s sha=%.7s", name, event, delivery, spec["mode"], spec["source_repo"], spec["sha"])
            self._reply(202, f"job {name}")

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    cfg = Config()
    cfg.validate()
    cfg.load_cluster()
    server = ThreadingHTTPServer(("", 8080), make_handler(cfg))
    server.timeout = 30
    log.info("listening on :8080, allowed repos: %d, central: %s, image: %s", len(cfg.allowed), cfg.central, cfg.image)
    server.serve_forever()


if __name__ == "__main__":
    main()
