import json

import pytest

from multisync.patching import PatchError, apply_operations, retained_pct, source_diff, split_sections, strip_generated
from multisync.pipeline import process_change
from multisync.testing import PASS_JUDGE, fake_llm, make_deps
from multisync.verify import verify_draft

PAGE = """Intro text before any heading.

# Listings API

Overview of the API.

## Endpoints

### GET /api/v1/listings

Returns listings.

```bash
# not a heading, a shell comment
curl /api/v1/listings
```

### POST /api/v1/listings

Creates a listing.
"""


def test_split_sections_headings_paths_preamble_and_fences():
    secs = split_sections(PAGE)
    assert [s["id"] for s in secs] == ["(preamble)", "Listings API", "Listings API > Endpoints", "Listings API > Endpoints > GET /api/v1/listings",
                                       "Listings API > Endpoints > POST /api/v1/listings"]
    assert "".join(s["text"] for s in secs) == PAGE
    assert "# not a heading" in secs[3]["text"]


def test_split_sections_repeated_headings_get_unique_ids_and_level_five_is_not_a_section():
    secs = split_sections("## A\n\nx\n\n## A\n\ny\n\n##### deep\n")
    assert [s["id"] for s in secs] == ["A", "A #2"]
    assert "##### deep" in secs[1]["text"]


def test_strip_generated_removes_front_matter_and_change_history():
    page = '---\ntitle: "T"\n---\n\n## A\n\nx\n\n## Change History\n\n| a |\n'
    assert strip_generated(page) == "## A\n\nx\n"


def test_apply_operations_untouched_sections_are_byte_identical_replace_insert_delete():
    secs = split_sections(PAGE)
    ops = [{"op": "replace", "section": "Listings API > Endpoints > GET /api/v1/listings", "text": "### GET /api/v1/listings\n\nReturns up to 100 listings."},
           {"op": "insert_after", "section": "Listings API > Endpoints > POST /api/v1/listings", "text": "### DELETE /api/v1/listings\n\nDeletes."},
           {"op": "delete", "section": "(preamble)"}]
    out = apply_operations(secs, ops)
    assert out.startswith("# Listings API\n\nOverview of the API.\n\n## Endpoints\n\n")
    assert "Returns up to 100 listings.\n\n### POST /api/v1/listings\n\nCreates a listing.\n\n### DELETE /api/v1/listings\n\nDeletes.\n" in out
    assert secs[1]["text"] in out and secs[2]["text"] in out and secs[4]["text"] in out
    assert apply_operations(secs, []) == PAGE


@pytest.mark.parametrize("ops", [[{"op": "replace", "section": "Nope", "text": "## Nope\n\nx"}],
                                 [{"op": "explode", "section": "Listings API"}],
                                 [{"op": "replace", "section": "Listings API", "text": "no heading here"}],
                                 [{"op": "delete", "section": "Listings API"}, {"op": "replace", "section": "Listings API", "text": "# Listings API\n\nx"}]])
def test_apply_operations_rejects_unknown_ids_bad_ops_and_conflicts(ops):
    with pytest.raises(PatchError):
        apply_operations(split_sections(PAGE), ops)


def test_retained_pct_and_source_diff():
    assert retained_pct("a\nb\nc\nd\n", "a\nb\nX\nd\n") == 75.0
    before = "### FILE: A.java\nint a = 50;\nint b = 60;\n\n### FILE: B.java\nx\n"
    after = "### FILE: A.java\nint a = 100;\nint b = 60;\n\n### FILE: B.java\nx\n"
    d = source_diff(before, after, ["A.java"])
    assert "-int a = 50;" in d and "+int a = 100;" in d and "B.java" not in d


# ── pipeline ─────────────────────────────────────────────────────────────────
def endpoint(name, extra=""):
    return (f"### {name}\n\nReturns the {name.split()[-1]} resource. Requires a bearer token.{extra}\n\n"
            f"| Parameter | Type | Description |\n|---|---|---|\n| page | integer | Page number |\n\n")


API_PAGE = ("# Listings API\n\nThis page describes the listings REST API and how to call it from a client application.\n\n"
            "## Authentication\n\nSend a bearer token in the Authorization header on every request.\n\n"
            "## Endpoints\n\n" + endpoint("GET /api/v1/listings", "\n\nThe page size is 50 and the maximum rate is 60 requests per minute.")
            + endpoint("GET /api/v1/listings/{id}") + endpoint("POST /api/v1/listings") + endpoint("DELETE /api/v1/listings/{id}")
            + "## Errors\n\nThe API answers 400 for invalid input and 401 without a valid token.\n\n"
            "## Pagination\n\nUse the page parameter to walk through results.\n\n"
            "## Versioning\n\nThe version is part of the path.\n")
JAVA_V1 = ("### FILE: src/ListingController.java\npublic class ListingController {\n    private static final int PAGE_SIZE = 50;\n"
           "    private static final int RATE_LIMIT = 60;\n    @GetMapping(\"/api/v1/listings\")\n    public List<Listing> list() { return service.all(); }\n}\n")
JAVA_V2 = JAVA_V1.replace("= 50;", "= 100;").replace("= 60;", "= 120;")
GET_ID = "Listings API > Endpoints > GET /api/v1/listings"


def unit(**o):
    return {"kind": "code", "repo": "o/r", "filePath": "api.md", "commit": "abc1234", "before": JAVA_V1, "after": JAVA_V2, "existing": API_PAGE,
            "changedFiles": ["src/ListingController.java"], "styleKey": "API documentation", **o}


def good_op(secs=None):
    sec = next(s for s in split_sections(API_PAGE) if s["id"] == GET_ID)
    return {"op": "replace", "section": GET_ID, "text": sec["text"].replace("size is 50", "size is 100").replace("is 60 requests", "is 120 requests").rstrip("\n")}


def note(d, node):
    return [t["note"] for t in d["trail"] if t["node"] == node]


def run(llm, **kw):
    return process_change(unit(**kw.pop("u", {})), make_deps(llm=llm, **kw))


def test_end_to_end_two_constants_change_touches_only_one_endpoint_section():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review", d.get("reason")
    assert llm.calls["patch"] == 1 and not llm.calls["drafts"], "no full rewrite"
    body = d["content"].split("\n---\n", 1)[1]
    # the page H1 becomes the front matter title, so compare everything below it
    old = split_sections(API_PAGE.split("\n", 2)[2])
    new = split_sections(strip_generated(body.lstrip("\n")))
    changed = [o["heading"] for o, n in zip(old, new) if o["text"] != n["text"]]
    assert len(old) == len(new) and changed == ["GET /api/v1/listings"]
    assert "size is 100" in body and "size is 50" not in body
    w = note(d, "write_draft")[-1]
    assert w["patchMode"] and w["sectionsTotal"] == len(old) and w["sectionsChanged"] == 1 and re_prompt(w["prompt"])
    v = note(d, "verify_draft")[-1]
    assert v["retainedPct"] >= 90
    m = d["metrics"]
    assert m["patchMode"] is True and m["sectionsChanged"] == 1 and m["sectionsTotal"] == len(old) and m["retainedPct"] >= 90
    assert "SECTION PATCH" not in llm.calls["patchInputs"][0] and "=== SECTION " + GET_ID in llm.calls["patchInputs"][0]
    assert "+    private static final int PAGE_SIZE = 100;" in llm.calls["patchInputs"][0]


def re_prompt(p):
    import re
    return re.match(r"^patch-code@[0-9a-f]{8}$", p)


def test_malformed_json_twice_falls_back_to_full_rewrite_and_records_patch_fallback():
    llm = fake_llm([PASS_JUDGE], patches=["nope", "still nope"])
    d = run(llm)
    assert llm.calls["patch"] == 2 and len(llm.calls["drafts"]) == 1
    w = note(d, "write_draft")[0]
    assert w["patchFallback"] is True and w["prompt"].startswith("draft-code@")
    assert d["metrics"]["patchMode"] is False and d["metrics"]["patchFallback"] is True


def test_malformed_json_once_is_retried():
    llm = fake_llm([PASS_JUDGE], patches=["nope", {"operations": [good_op()]}])
    d = run(llm)
    assert llm.calls["patch"] == 2 and not llm.calls["drafts"] and d["metrics"]["patchMode"] is True


def test_unknown_section_id_falls_back():
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": [{"op": "replace", "section": "Nope", "text": "## Nope\n\nx"}]}])
    d = run(llm)
    assert llm.calls["patch"] == 1 and len(llm.calls["drafts"]) == 1
    assert note(d, "write_draft")[0]["patchFallback"] is True


def test_patch_too_broad_is_rejected_with_feedback_then_redone():
    secs = split_sections(API_PAGE)
    broad = [{"op": "replace", "section": s["id"], "text": s["text"].rstrip("\n") + "\n\nExtra sentence: the limit is `MAX_ITEMS` = 77."} for s in secs[1:7]]
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": broad}, {"operations": [good_op()]}])
    d = run(llm)
    assert d["outcome"] == "pending_review"
    vs = note(d, "verify_draft")
    assert vs[0]["ok"] is False and vs[0]["reasons"][0].startswith("patch_too_broad")
    assert llm.calls["patch"] == 2 and "patch_too_broad" in llm.calls["patchInputs"][1]
    assert d["metrics"]["sectionsChanged"] == 1


def test_guard_does_not_fire_for_a_large_source_change_or_a_tiny_page():
    base = {"base": "x\n", "sectionsTotal": 8, "sectionsChanged": 6, "diffLines": 200, "maxShare": 0.5, "smallLines": 20, "minSections": 4}
    text = API_PAGE
    assert verify_draft(text, patch=base)["ok"]
    assert not verify_draft(text, patch={**base, "diffLines": 4})["ok"]
    assert verify_draft(text, patch={**base, "diffLines": 4, "sectionsTotal": 3, "sectionsChanged": 3})["ok"]


FULL = API_PAGE.replace("# Listings API\n\n", "", 1)


def test_first_draft_and_full_sync_and_docs_mode_keep_the_full_rewrite_path():
    for u in ({"before": None}, {"before": ""}):
        llm = fake_llm([PASS_JUDGE], draft=FULL, patches=[{"operations": []}])
        d = run(llm, u=u)
        assert llm.calls["patch"] == 0 and len(llm.calls["drafts"]) == 1
        assert d["metrics"]["patchMode"] is False
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": []}])
    run(llm, env={"PATCH_DRAFTING": "0"})
    assert llm.calls["patch"] == 0
    llm = fake_llm([PASS_JUDGE], patches=[{"operations": []}])
    process_change({"kind": "docs", "repo": "o/r", "filePath": "a.md", "commit": "abc1234", "before": "## Overview\n\nAlert API.\n\n- email\n",
                    "after": "## Overview\n\nAlert API on port 8080.\n\n- email\n- slack\n- pagerduty\n\n## Configuration\n\nSet `ALERT_PORT`.\n"}, make_deps(llm=llm))
    assert llm.calls["patch"] == 0
    assert json.dumps(llm.calls["log"]).count("patch") == 0
