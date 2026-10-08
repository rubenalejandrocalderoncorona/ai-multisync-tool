"""Files that enter or leave a page's scope: sensitive ones are invisible to change detection, symbol-less ones carry no documentation
obligation, real public changes still produce a draft. Generic file properties only; no path in the product code is special."""
import pytest

from multisync.codesource import build_code_changes, redact_sensitive_paths
from multisync.critic import judge
from multisync.pipeline import process_change
from multisync.prefilter import prefilter
from multisync.testing import PASS_JUDGE, fake_llm, make_deps

BASE = {
    "src/api/Ctl.java": '@RestController\npublic class Ctl {\n  @GetMapping("/api/v1/a")\n  public String a() { return ""; }\n}\n',
    "README.md": "# Project\n\nA small service.\n",
}
NEW_ROUTE = '@RestController\npublic class Other {\n  @PostMapping("/api/v1/orders")\n  public String o() { return ""; }\n}\n'
POLICY = {"pages": [{"path": "api.md", "kind": "API documentation", "scope": ["**"]}]}


def stub(t1, t2):
    tree = {"c1": t1, "c2": t2}
    return {"list_files": lambda rev: list(tree.get(rev, {})), "read_at": lambda rev, f: tree.get(rev, {}).get(f),
            "changed_between": lambda a, b: sorted(f for f in {*tree[a], *tree[b]} if tree[a].get(f) != tree[b].get(f)),
            "read_existing_page": lambda page: "# Old page\n"}


DRAFT = ("## Overview\n\nThe service exposes `GET /api/v1/a` and `POST /api/v1/orders` through the Ctl and Other controllers. A call to the orders "
         "endpoint creates an order and returns a string.\n\n## Configuration\n\nThe datasource is set with `spring.datasource.url` and the page size with "
         "`app.page-size`, which defaults to 50 entries per page and applies to every listing returned by the service.\n")


def run(t1, t2, judges=None):
    units = build_code_changes(repo="o/r", policy=POLICY, commit="c2", before="c1", **stub(t1, t2))
    llm = fake_llm(judges or [PASS_JUDGE], draft=DRAFT)
    decisions = [process_change(u, make_deps(llm=llm)) for u in units]
    return units, llm, decisions


def calls(llm):
    return llm.calls["chat"] + llm.calls["embed"] + llm.calls["judge"]


SKIPPED = [
    ("env sample", {".env.sample": "A=1\n"}, "remove"),
    ("pem", {"certs/server.pem": "-----BEGIN-----\n"}, "remove"),
    ("secrets dir", {"secrets/prod.json": '{"a": 1}'}, "remove"),
    ("service account", {"config/service-account.json": '{"type": "x"}'}, "remove"),
    ("credentials yaml", {"src/api/error/credentials.yml": "user: a\n"}, "remove"),
    ("confidential yaml", {"config/data.yml": "Classification: Confidential\nrows: 3\n"}, "remove"),
    ("confidential yaml added", {"config/data.yml": "Classification: Confidential\nrows: 3\n"}, "add"),
    ("csv removed", {"data/people.csv": "id,name\n1,Ann\n2,Bob\n3,Cy\n"}, "remove"),
    ("csv added", {"data/people.csv": "id,name\n1,Ann\n2,Bob\n3,Cy\n"}, "add"),
    ("notes removed", {"src/notes.txt": "Remember to rotate things in 30 days.\nSecond line 42.\nThird line.\n"}, "remove"),
    ("notes added", {"src/notes.txt": "Remember to rotate things in 30 days.\nSecond line 42.\nThird line.\n"}, "add"),
    ("private helper removed", {"src/util/helper.py": "def _tidy(x):\n    return x.strip()\n\ndef _more(y):\n    return y\n"}, "remove"),
]
SENSITIVE_NAMES = {"env sample", "pem", "secrets dir", "service account", "credentials yaml", "confidential yaml", "confidential yaml added"}


@pytest.mark.parametrize("name,files,how", SKIPPED, ids=[s[0] for s in SKIPPED])
def test_churn_without_public_symbols_or_with_sensitive_files_never_calls_a_model(name, files, how):
    t1, t2 = dict(BASE), dict(BASE)
    (t1 if how == "remove" else t2).update(files)
    units, llm, decisions = run(t1, t2)
    assert calls(llm) == 0, name
    if name in SENSITIVE_NAMES:
        assert units == [], "a sensitive file is not a change at all"
    for d in decisions:
        assert d["outcome"] == "skipped" and d["rootCauseTag"] == "no_documented_change"
        assert not any(p in d["reason"] for p in files), "the reason never names a file"
        assert "1 added" in d["reason"] or "1 removed" in d["reason"]


def test_a_readme_that_only_loses_its_confidential_marker_line_is_ignored(capsys):
    t1 = {**BASE, "README.md": "# Project\n\nClassification: Confidential\n\nA small service.\n"}
    units, llm, decisions = run(t1, dict(BASE))
    assert units == [] and calls(llm) == 0
    assert "1 sensitive-looking file(s) ignored" in capsys.readouterr().out


def test_sensitive_only_change_yields_no_change_unit_and_logs_a_count_not_names(capsys):
    t1 = {**BASE, "secrets/prod.json": "{}", "keys/id_rsa": "x"}
    assert build_code_changes(repo="o/r", policy=POLICY, commit="c2", before="c1", **stub(t1, dict(BASE))) == []
    out = capsys.readouterr().out
    assert "2 sensitive-looking file(s) ignored" in out and "prod.json" not in out and "id_rsa" not in out


def test_control_a_new_source_file_with_a_route_still_produces_a_draft():
    units, llm, decisions = run(BASE, {**BASE, "src/api/Other.java": NEW_ROUTE})
    assert decisions[0]["outcome"] == "pending_review" and calls(llm) > 0
    assert units[0]["noObligationFiles"] == []


def test_control_a_changed_public_function_signature_still_produces_a_draft():
    t1 = {**BASE, "src/lib/paging.ts": "export function listOrders(status: string) {\n  return [];\n}\n"}
    t2 = {**BASE, "src/lib/paging.ts": "export function listOrders(status: string, page: number) {\n  return [];\n}\n"}
    _, llm, decisions = run(t1, t2)
    assert decisions[0]["outcome"] != "skipped" and calls(llm) > 0


def test_control_a_real_config_key_change_in_application_properties_still_produces_a_draft():
    p1 = {**BASE, "src/main/resources/application.properties": "spring.datasource.url=jdbc:x\n"}
    p2 = {**BASE, "src/main/resources/application.properties": "spring.datasource.url=jdbc:x\napp.page-size=50\n"}
    _, llm, decisions = run(p1, p2)
    assert decisions[0]["outcome"] != "skipped" and calls(llm) > 0


def test_a_deleted_file_with_public_symbols_is_still_a_change():
    _, llm, decisions = run({**BASE, "src/api/Other.java": NEW_ROUTE}, BASE)
    assert decisions[0]["outcome"] != "skipped" and calls(llm) > 0


def test_mixed_commit_documents_the_route_and_never_mentions_the_removed_sensitive_file():
    gone = "src/api/error/credentials.yml"
    t1 = {**BASE, gone: "user: a\npass: b\n"}
    t2 = {**BASE, "src/api/Other.java": NEW_ROUTE}
    units, llm, decisions = run(t1, t2)
    d = decisions[0]
    assert d["outcome"] == "pending_review"
    everything = "\n".join([*llm.calls["drafts"], *llm.calls["analyzeInputs"], *llm.calls["planInputs"], *llm.calls.get("judgeInputs", []), d["reason"] or "",
                            d.get("content") or "", str(units[0]["changedFiles"]), str(units[0]["repoMap"]), units[0]["before"] or "", units[0]["after"]])
    assert "credentials" not in everything and "/api/v1/orders" in everything


def test_the_prefilter_ignores_the_snapshot_header_lines():
    a = "### FILE: a.txt\nhello\n\n"
    r = prefilter(a, a + "### FILE: b.txt\nworld wide\n\n", 3)
    assert r["proceed"] is False and r["tag"] == "no_documented_change" and "1 added" in r["reason"]
    # a header is not a "heading" change on a tiny diff: a one-line wording change stays trivial
    assert prefilter(a, a.replace("hello", "hello there"), 3)["tag"] == "trivial_diff"


def test_first_documentation_of_a_page_is_not_treated_as_churn():
    assert prefilter("", "### FILE: a.txt\nport 8080\nhello\nworld\n\n", 3)["proceed"] is True


def test_judge_does_not_owe_a_claim_for_a_removed_file_without_public_symbols():
    demand = {**PASS_JUDGE, "facts": [{"text": "The file src/util/helper.py was removed.", "covered": False, "core": False},
                                      {"text": "port 8080", "covered": True}]}
    llm = fake_llm([demand])
    v = judge(llm, mode="code", source="x", draft="d", changed_files=["src/util/helper.py (removed)"], no_obligation_files=["src/util/helper.py"])
    assert v["missing"] == [] and v["recall"] == 1
    assert "OUT_OF_SCOPE_FILES" in llm.calls["judgeInputs"][0]
    # without the exemption the same model reply would fail the draft
    assert judge(fake_llm([demand]), mode="code", source="x", draft="d")["missing"]


def test_pipeline_passes_the_exemption_to_the_judge():
    t1 = {**BASE, "data/people.csv": "id\n1\n"}
    t2 = {**BASE, "src/api/Other.java": NEW_ROUTE}
    demand = {**PASS_JUDGE, "facts": [{"text": "data/people.csv was removed.", "covered": False}, {"text": "port 8080", "covered": True}]}
    units, llm, decisions = run(t1, t2, judges=[demand])
    assert units[0]["noObligationFiles"] == ["data/people.csv"]
    assert decisions[0]["outcome"] == "pending_review"


def test_paths_that_look_sensitive_are_redacted_from_public_strings():
    assert redact_sensitive_paths("see src/api/error/credentials.yml and src/a.go") == "see [sensitive file] and src/a.go"
