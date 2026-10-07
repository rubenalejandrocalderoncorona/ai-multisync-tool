from multisync.prefilter import prefilter

JAVA = "### FILE: src/A.java\nclass A {\n    private static final int MAX_PAGE_SIZE = 50;\n    int f() { return 1; }\n}\n\n"
ROUTE = '### FILE: src/C.java\n@RestController\npublic class C {\n  @GetMapping("/api/v1/x")\n  public String x() { return ""; }\n}\n\n'


def test_one_line_that_changes_a_documented_number_is_processed_and_forced():
    r = prefilter(JAVA, JAVA.replace("= 50;", "= 100;"), 3)
    assert r["proceed"] is True and r["forced"] is True
    assert r["metrics"]["diff"]["total"] == 2 and r["metrics"]["smallFactChange"] is True
    assert "fact_tokens" in r["reason"]


def test_a_small_wording_only_change_is_still_dropped():
    r = prefilter(JAVA, JAVA.replace("int f() { return 1; }", "int f() { return 1; } // helper"), 3)
    assert r["proceed"] is False and r["tag"] == "trivial_diff"


def test_a_small_change_to_a_public_route_is_processed():
    r = prefilter(ROUTE, ROUTE.replace("/api/v1/x", "/api/v1/y"), 3)
    assert r["proceed"] is True and r["forced"] is True


def test_the_normal_path_is_unchanged_for_diffs_at_or_above_the_minimum():
    after = JAVA.replace("= 50;", "= 100;").replace("return 1;", "return 2;\n    // more")
    r = prefilter(JAVA, after, 3)
    assert r["proceed"] is True and "smallFactChange" not in r["metrics"]


def _java(line):
    return "### FILE: src/B.java\nclass B {\n    " + line + "\n    int g() { return 1; }\n}\n\n"


def test_a_duration_literal_change_is_a_fact_change():
    r = prefilter(_java('static final String TTL = "30m";'), _java('static final String TTL = "15m";'), 3)
    assert r["proceed"] is True and r["forced"] is True and "fact_tokens" in r["reason"]


def test_underscore_numbers_are_numbers():
    r = prefilter(_java("static final int MAX = 10_000;"), _java("static final int MAX = 20_000;"), 3)
    assert r["proceed"] is True and r["forced"] is True
    same = prefilter(_java("static final int MAX = 10_000;"), _java("static final int MAX = 10000;"), 3)
    assert same["proceed"] is False  # the same number written differently is not a change


def test_an_edited_message_the_page_quotes_is_processed_only_when_the_page_repeats_it():
    old = _java('static final String MSG = "Idempotency key was already used with a different request";')
    new = _java('static final String MSG = "Idempotency key was already used with a different payload";')
    page = "Replaying a key fails with: Idempotency key was already used with a different request."
    quoted = prefilter(old, new, 3, page)
    assert quoted["proceed"] is True and quoted["forced"] is True and quoted["metrics"]["quotedText"]
    assert prefilter(old, new, 3, "The page never quotes this message.")["proceed"] is False
    assert prefilter(old, new, 3)["proceed"] is False  # no page text: behaves as before


def test_a_log_message_the_page_does_not_repeat_stays_dropped_at_any_size():
    old = _java('log.info("starting the cleanup job now");') 
    new = _java('log.info("starting the cleanup run now");')
    assert prefilter(old, new, 3, "Unrelated page text about listings.")["proceed"] is False
