"""Runs the real CLI (python -m multisync.cli.run_pipeline) as a subprocess against a real git repository and a local OpenAI-compatible
HTTP server. Verifies the contract the Job and the workflow depend on: git plumbing, mode selection, files written, pipeline-results.json,
node logs on stdout."""
import json
import os
import re
import subprocess
import sys

import pytest

from multisync.testing import FakeOpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A_V1 = "package main\nfunc Alert() {}\n"
A_V2 = 'package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n'


def git(src, *a):
    return subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True, text=True).stdout


def init_repo(src):
    (src / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    git(src, "config", "user.email", "t@t")
    git(src, "config", "user.name", "t")


def commit(src, msg):
    git(src, "add", "-A")
    git(src, "commit", "-qm", msg)
    return git(src, "rev-parse", "HEAD").strip()


def run_cli(work, env, module="multisync.cli.run_pipeline"):
    p = subprocess.run([sys.executable, "-m", module], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def base_env(work, url, repos, **extra):
    (work / "repos.json").write_text(json.dumps(repos))
    return {**os.environ, "PYTHONPATH": ROOT, "AI_API_BASE_URL": url, "INTERNAL_AI_API_KEY": "x", "VECTOR_DRIVER": "memory", "FACTSTORE_DRIVER": "memory",
            "REPOS_CONFIG": str(work / "repos.json"), "FEATURE_REGISTRY": str(work / "none.json"), "DOCS_ROOT": "site/docs",
            "INSTRUCTIONS_FILE": os.path.join(ROOT, ".github/instructions/DocumentationInstructions.instructions.md"),
            "TEMPLATES_PATH": os.path.join(ROOT, "docs/templates"), "SOURCE_REPO": "o/proj", "SOURCE_DIR": str(work / "source-repo"), "TICKET_PROVIDER": "none", **extra}


def test_cli_code_mode_end_to_end_real_git_repo_graph_page_on_disk_results_and_logs(tmp_path):
    fake = FakeOpenAI()
    src = tmp_path / "source-repo"
    try:
        init_repo(src)
        (src / "src/a.go").write_text(A_V1)
        (src / "src/a_test.go").write_text("package main\nfunc TestAlert() {}\n")
        c1 = commit(src, "one")
        (src / "src/a.go").write_text(A_V2)
        c2 = commit(src, "two")
        env = base_env(tmp_path, fake.url, {"repos": {"o/proj": {"mode": "code", "trust": "auto", "serviceName": "proj", "pages": [{"path": "api.md", "kind": "API documentation", "scope": ["src/**"]}]}}},
                       SOURCE_SHA=c2, SOURCE_BEFORE=c1, CHANGED_FILES="")
        code, out = run_cli(tmp_path, env)
        assert code == 0, out
        assert re.search(r"mode=code \| 1 change unit", out)
        for node in ("sync_context", "prefilter", "cross_repo", "route", "similarity", "code_context", "gar", "semantic_context", "write_draft", "verify_draft", "judge", "publish"):
            assert f"[{node}]" in out
        assert re.search(r'\[sync_context\] ok \d+ms \{"mode": "full"', out)

        results = json.loads((tmp_path / "pipeline-results.json").read_text())
        assert len(results["results"]) == 1
        d = results["results"][0]
        assert d["outcome"] == "published"
        assert d["mode"] == "code"
        assert d["style"] == "API documentation"
        assert [s.split(":")[0] for s in d["stages"]] == ["prefilter", "cross_repo", "route", "similarity", "code_context", "gar", "semantic_context", "write_draft", "verify_draft", "judge", "publish"]

        page = (tmp_path / d["targetPath"]).read_text()
        assert d["targetPath"] == "site/docs/services/proj/api.md"
        assert page.startswith("---\ntitle: ")
        assert "doc_key: api.md" in page
        assert "TestAlert" not in page, "test files never reach the model or the page"
        assert fake.hits["judge"] >= 1 and fake.hits["embed"] >= 1
        assert fake.hits["analyze"] == 1, "code context stage ran once"
        assert fake.hits["plan"] == 1, "semantic context stage ran once"
    finally:
        fake.close()


def test_cli_fails_safe_model_unreachable_becomes_a_pipeline_error_fallback_nothing_is_written(tmp_path):
    src = tmp_path / "source-repo"
    init_repo(src)
    (src / "src/a.go").write_text("package main\nfunc A() {}\nfunc B() {}\nfunc C() {}\n")
    c = commit(src, "one")
    env = base_env(tmp_path, "http://127.0.0.1:9", {"repos": {"o/proj": {"mode": "code", "serviceName": "proj"}}}, AI_TIMEOUT_MS="1500", AI_MAX_RETRIES="0",
                   SOURCE_SHA=c, SOURCE_BEFORE="0" * 40)
    code, out = run_cli(tmp_path, env)
    assert code == 0, out
    d = json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]
    assert d["outcome"] == "fallback"
    assert d["rootCauseTag"] == "pipeline_error"
    assert not (tmp_path / "site").exists(), "no page is written on a pipeline error"


def test_cli_a_protected_target_branch_forces_review_even_for_a_trust_auto_repo(tmp_path):
    fake = FakeOpenAI()
    src = tmp_path / "source-repo"
    try:
        init_repo(src)
        (src / "src/a.go").write_text(A_V1)
        c1 = commit(src, "one")
        (src / "src/a.go").write_text(A_V2)
        c2 = commit(src, "two")
        env = base_env(tmp_path, fake.url, {"repos": {"o/proj": {"mode": "code", "trust": "auto", "serviceName": "proj", "pages": [{"path": "api.md", "scope": ["src/**"]}]}}}, SOURCE_SHA=c2, SOURCE_BEFORE=c1)
        code, out = run_cli(tmp_path, {**env, "TARGET_BRANCH": "staging"})
        assert json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]["outcome"] == "published", out
        code, out = run_cli(tmp_path, {**env, "TARGET_BRANCH": "main"})
        assert "trust forced from auto to review" in out
        assert json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]["outcome"] == "pending_review"
    finally:
        fake.close()


def sensitive_repo(tmp_path, extra_config=None):
    src = tmp_path / "source-repo"
    init_repo(src)
    (src / "src/a.go").write_text(A_V1)
    (src / "NOTES.md").write_text("---\nclassification: Oracle Restricted\n---\ninternal planning notes\n")
    c1 = commit(src, "one")
    (src / "src/a.go").write_text(A_V2)
    c2 = commit(src, "two")
    entry = {"mode": "code", "trust": "auto", "serviceName": "proj", "pages": [{"path": "api.md", "kind": "API documentation", "scope": ["**"], "brief": "The API."}], **(extra_config or {})}
    return src, c1, c2, {"repos": {"o/proj": entry}}


def test_a_repository_with_a_sensitive_file_in_scope_is_blocked_before_anything_is_embedded_or_sent(tmp_path):
    fake = FakeOpenAI()
    try:
        src, c1, c2, repos = sensitive_repo(tmp_path)
        env = base_env(tmp_path, fake.url, repos, SOURCE_SHA=c2, SOURCE_BEFORE=c1)
        code, out = run_cli(tmp_path, env)
        assert code == 0, out
        assert "onboarding blocked, nothing is embedded or sent to a model" in out
        d = json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]
        assert d["outcome"] == "fallback" and d["rootCauseTag"] == "onboarding_blocked" and "look sensitive" in d["reason"]
        # names stay in the Job log and the decision store, never in the reason that tickets and alerts carry
        assert "NOTES.md" not in d["reason"] and "NOTES.md" in out and "NOTES.md" in d["metrics"]["onboardingBlockers"]
        assert fake.hits["embed"] == 0 and fake.hits["chat"] == 0, "no text left the machine"
        assert not (tmp_path / "site").exists()
    finally:
        fake.close()


def test_the_owner_can_acknowledge_a_sensitive_looking_file_and_the_run_proceeds(tmp_path):
    fake = FakeOpenAI()
    try:
        src, c1, c2, repos = sensitive_repo(tmp_path, {"allowSensitive": ["NOTES.md"]})
        env = base_env(tmp_path, fake.url, repos, SOURCE_SHA=c2, SOURCE_BEFORE=c1)
        code, out = run_cli(tmp_path, env)
        assert code == 0 and "onboarding blocked" not in out, out
        assert json.loads((tmp_path / "pipeline-results.json").read_text())["results"][0]["outcome"] == "published"
        assert fake.hits["embed"] >= 1
    finally:
        fake.close()
