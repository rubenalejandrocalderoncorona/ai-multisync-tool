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
