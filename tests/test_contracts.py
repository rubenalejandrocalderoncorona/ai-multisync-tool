import pytest

from multisync.cli.onboard_check import plan_onboarding
from multisync.contracts import consumer_repos, gate_mode, linked_repos, normalize_route, routes_match, validate_links
from multisync.factstore import MemoryFactStore
from multisync.pipeline import process_change
from multisync.router import route_change
from multisync.symbols import diff_public_symbols, extract_public_symbols, public_symbols
from multisync.testing import fake_llm, make_deps, PASS_JUDGE

BE, FE = "Fidavia/fidavia", "Fidavia/fidavia-frontend"


# ── normalization and matching ───────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("/api/v1/events/{id}/venue", "/api/v1/events/{}/venue"),
    ("/api/v1/events/{eventId}/venue", "/api/v1/events/{}/venue"),
    ("/api/v1/events/:id/venue", "/api/v1/events/{}/venue"),
    ("/api/v1/events/<int:id>/venue", "/api/v1/events/{}/venue"),
    ("/api/v1/events/${id}/venue", "/api/v1/events/{}/venue"),
    ("/api/v1/events/{id:[0-9]+}/venue", "/api/v1/events/{}/venue"),
    ("GET /API/V1/Events/{id}/", "/api/v1/events/{}"),
    ("/api/v1/events?page=2&size=5", "/api/v1/events"),
    ("https://host.example.com:8080/api/v1/events/{id}?x=1", "/api/v1/events/{}"),
    ("/", "/"),
])
def test_normalize_route(raw, want):
    assert normalize_route(raw) == want


@pytest.mark.parametrize("provider,consumer,ok", [
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/{eventId}/venue", True),
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/:id/venue", True),
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/${id}/venue", True),
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/{id}/venue/", True),
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/{id}/venue?full=true", True),
    ("POST /api/v1/events", "/api/v1/events", True),
    ("GET /api/v1/events/{id}/venue", "/api/v1/events/{id}", False),            # fewer segments
    ("GET /api/v1/events/{id}", "/api/v1/events/{id}/venue", False),            # more segments
    ("GET /api/v1/events", "/api/v1/events-archive", False),                    # similar prefix
    ("GET /api/v1/events", "/api/v1/events/{}/venue", False),
    ("GET /api/v1/event/{id}", "/api/v1/events/{id}", False),
    ("GET /", "/", False),
])
def test_routes_match(provider, consumer, ok):
    assert routes_match(provider, consumer) is ok


# ── client_call extraction ───────────────────────────────────────────────────
FE_JAVA = '''package mx.fidavia.frontend;

@Service
public class VenueClient {
    private static final String EVENT_VENUE_PATH = "/api/v1/events/{id}/venue";
    private static final String BASE = "/api/v1";
    private static final String EVENTS = BASE + "/events/{id}";
    private final RestClient rest;

    public Venue venue(long id) {
        return get(EVENT_VENUE_PATH, id);
    }

    public Event event(long id) {
        return rest.get().uri("/api/v1/events/{id}/summary", id).retrieve().body(Event.class);
    }

    public Event partial(long id) {
        return rest.get().uri("/api/v1/events" + "/" + id).retrieve().body(Event.class);
    }

    // rest.get().uri("/api/v1/commented/out")
    String log = "/api/v1/not/a/call/but/api-prefixed is fine?";
    String asset = "/static/js/app.js";
    String root = "/health";
}
'''


def calls(file, text):
    return sorted(s["name"] for s in extract_public_symbols(file, text, calls=True) if s["kind"] == "client_call")


def test_java_client_calls_from_the_real_frontend_snippet():
    got = calls("VenueClient.java", FE_JAVA)
    assert "/api/v1/events/{id}/venue" in got
    assert "/api/v1/events/{id}/summary" in got
    assert "/api/v1/events/{id}" in got                     # BASE + "/events/{id}" resolved as a constant
    assert "/api/v1" not in got                             # a base path is not a call
    assert "/api/v1/events" not in got                      # "/api/v1/events" + ... is a piece of a concatenation
    assert "/api/v1/commented/out" not in got
    assert "/static/js/app.js" not in got and "/health" not in got


def test_java_served_routes_are_not_client_calls():
    ctrl = '''@RestController
@RequestMapping("/api/v1/events")
public class EventController {
    private static final String VENUE = "/{id}/venue";
    @GetMapping(VENUE)
    public Venue v(@PathVariable long id) { return null; }
    @GetMapping("/{id}/other")
    public Venue o(@PathVariable long id) { return null; }
}
'''
    assert calls("EventController.java", ctrl) == []
    served = ctrl.replace('"/{id}/venue"', '"/api/v1/events/{id}/venue"')
    assert calls("EventController.java", served) == []     # constant feeding a mapping annotation


def test_client_calls_do_not_leak_into_public_symbols_or_diffs():
    assert not [s for s in extract_public_symbols("VenueClient.java", FE_JAVA) if s["kind"] == "client_call"]
    snap = "### FILE: VenueClient.java\n" + FE_JAVA
    assert not [k for k in public_symbols(snap) if k.startswith("client_call:")]
    d = diff_public_symbols(snap, snap.replace("/summary", "/summary2"))
    assert d["touched"] is False


def test_js_and_python_client_calls():
    js = '''import axios from "axios";
const BASE = import.meta.env.VITE_API;
export async function load(id) {
  await fetch("/api/v1/events/" + id);
  await fetch(`${BASE}/api/v1/events/${id}/venue`);
  await axios.get("/api/v1/tickets/{id}");
  const href = "/dashboard/settings";
  const img = "/static/logo.png";
}
app.get("/api/v1/served/{id}", h);
'''
    assert calls("client.ts", js) == ["/api/v1/events/${id}/venue", "/api/v1/tickets/{id}"]
    py = '''import requests, httpx
def go(i):
    requests.get("/api/v1/events/%s/venue" % i)
    httpx.post(f"{BASE}/api/v1/orders/{i}/pay")
    url = "/etc/passwd"

@router.get("/api/v1/served/{i}")
def served(i): ...
'''
    assert calls("c.py", py) == ["/api/v1/events/%s/venue", "/api/v1/orders/{i}/pay"]


# ── config: links ────────────────────────────────────────────────────────────
CFG = {"defaults": {}, "repos": {BE: {"linkedRepos": [FE], "contract": {"role": "provider"}}, FE: {"contract": {"role": "consumer"}}, "o/other": {}}}


def test_links_are_symmetrical_and_roles_filter_direction():
    assert linked_repos(CFG, BE) == [FE]
    assert linked_repos(CFG, FE) == [BE]          # B never lists A, still linked
    assert consumer_repos(CFG, BE) == [FE]
    assert consumer_repos(CFG, FE) == []          # a consumer-only repo never gates
    assert linked_repos(CFG, "o/other") == []


def test_config_validation_warns_and_never_raises():
    bad = {"repos": {"a/a": {"linkedRepos": ["a/a", "nope", "x/missing", "b/b"], "contract": {"role": "weird"}, "crossRepoGate": "maybe"},
                     "b/b": {"linkedRepos": ["a/a"]}, "c/c": {"linkedRepos": "a/a", "contract": 3}}}
    w = " | ".join(validate_links(bad))
    for part in ("lists the repo itself", "'nope' is not", "x/missing has no entry", "declared on both sides", "contract.role", "crossRepoGate", "must be a list", "contract must be an object"):
        assert part in w, part
    assert validate_links(None) == [] and validate_links({}) == []
    assert len(linked_repos({"repos": {"a/a": {"linkedRepos": [f"o/r{i}" for i in range(50)]}}}, "a/a")) == 10
    assert validate_links(CFG) == []


def test_gate_mode_env_wins_and_garbage_means_block():
    assert gate_mode({}) == "block"
    assert gate_mode({"crossRepoGate": "warn"}) == "warn"
    assert gate_mode({"crossRepoGate": "warn"}, "off") == "off"
    assert gate_mode({"crossRepoGate": "nonsense"}, "") == "block"


def test_onboard_check_reports_link_problems_as_warnings():
    class Tree:
        def list_files(self, rev): return ["a.java"]
        def read_at(self, rev, f): return "class A {}"
    pol = {"trust": "review", "serviceName": "s", "pages": [{"path": "o.md", "scope": ["*.java"], "brief": "b"}], "glossary": {"a": "b"}, "docs": ["x"]}
    r = plan_onboarding("a/a", pol, Tree(), "abc1234", None, {"repos": {"a/a": {"linkedRepos": ["x/missing"]}}})
    assert any("x/missing has no entry" in w for w in r["warnings"]) and not r["blockers"]


# ── gate ─────────────────────────────────────────────────────────────────────
CTRL_V1 = '''### FILE: src/EventController.java
@RestController
@RequestMapping("/api/v1/events")
public class EventController {
    @GetMapping("/{id}")
    public Event one(@PathVariable long id) { return null; }
}
'''
CTRL_V2 = CTRL_V1.rstrip("\n")[:-1] + '''    @GetMapping("/{id}/venue")
    public Venue venue(@PathVariable long id) { return null; }
}
'''


def seed_consumer(facts, repo=FE, text=FE_JAVA, path="VenueClient.java"):
    facts.replace_symbols(repo, [{"path": path, "kind": s["kind"], "name": s["name"], "sig_hash": ""}
                                 for s in extract_public_symbols(path, text, calls=True)])


def unit(repo=BE, before=CTRL_V1, after=CTRL_V2, **o):
    return {"kind": "code", "repo": repo, "filePath": "overview.md", "commit": "abc1234", "before": before, "after": after, "existing": "# Old\n",
            "changedFiles": ["src/EventController.java"], "brief": "Explain the events API", "repoMap": ["src/EventController.java"], **o}


def deps(env=None, policy=None, facts=None, repos_config=CFG, **kw):
    d = make_deps(env={"MIN_DIFF_LINES": "1", **(env or {})}, llm=fake_llm([PASS_JUDGE]), facts=facts or MemoryFactStore(), policy={"linkedRepos": [FE], **(policy or {})}, **kw)
    if repos_config is not None:
        d["reposConfig"] = repos_config
    return d


def cross(d):
    return next(t for t in d["trail"] if t["node"] == "cross_repo")


def test_provider_route_change_with_undocumented_linked_consumer_is_blocked():
    dp = deps()
    seed_consumer(dp["facts"])
    d = process_change(unit(), dp)
    assert d["outcome"] == "fallback" and d["rootCauseTag"] == "cross_repo_incomplete"
    assert FE in d["reason"] and "GET /api/v1/events/{id}/venue" in d["reason"]
    assert dp["llm"].calls["chat"] == 0
    cr = d["metrics"]["crossRepo"]
    assert cr["mode"] == "linked"
    assert cr["points"][0]["route"] == "GET /api/v1/events/{id}/venue" and cr["points"][0]["consumers"] == [FE] and cr["points"][0]["missing"] == [FE]


def test_gate_passes_once_the_consumer_has_an_approved_document_for_its_call():
    dp = deps()
    seed_consumer(dp["facts"])
    dp["facts"].save_claims(FE, "docs/venue.md", "f1", [{"text": "The app calls /api/v1/events/{id}/venue to load the venue", "supported": True}])
    dp["facts"].approve_claims(FE, "docs/venue.md", "f1")
    d = process_change(unit(), dp)
    assert cross(d)["status"] != "stop" and d.get("rootCauseTag") != "cross_repo_incomplete"
    assert d["metrics"]["crossRepo"]["points"][0]["missing"] == []
    assert d["metrics"]["diffClassification"] == "public_interface"


def test_gate_does_not_block_when_nobody_calls_the_new_route():
    dp = deps()
    seed_consumer(dp["facts"], text="class X { String a = \"/api/v1/other/{id}/thing\"; }")
    d = process_change(unit(), dp)
    assert cross(d)["status"] == "skip" and d.get("rootCauseTag") != "cross_repo_incomplete"
    dp2 = deps()  # the consumer has no symbols at all
    assert cross(process_change(unit(), dp2))["status"] == "skip"


def test_consumer_change_never_blocks_on_the_provider():
    dp = deps(policy={"linkedRepos": [BE]}, repos_config=CFG)
    seed_consumer(dp["facts"])
    d = process_change(unit(repo=FE, before=CTRL_V1, after=CTRL_V2), dp)
    assert cross(d)["status"] == "skip"


def test_unlinked_repos_are_never_gated():
    dp = deps(policy={"linkedRepos": []}, repos_config={"repos": {"o/other": {}}})
    seed_consumer(dp["facts"])
    d = process_change(unit(repo="o/other"), dp)
    assert cross(d)["status"] == "skip" and d.get("rootCauseTag") != "cross_repo_incomplete"


def test_first_snapshot_without_before_does_not_block():
    dp = deps()
    seed_consumer(dp["facts"])
    assert cross(process_change(unit(before=None), dp))["status"] == "skip"


def test_manual_registry_entry_still_works_unchanged():
    reg = {"alert_channels_enum": {"owner": BE, "requires": ["org/frontend"]}}
    dp = deps(registry=reg, policy={"linkedRepos": []}, repos_config=None)
    d = process_change(unit(after=CTRL_V1 + "\n// alert_channels_enum\nclass K { int x = 1; String s = \"alert_channels_enum\"; }\n"), dp)
    assert d["outcome"] == "fallback" and d["rootCauseTag"] == "cross_repo_incomplete" and "alert_channels_enum awaiting org/frontend" in d["reason"]
    assert "mode" not in d["metrics"]["crossRepo"]


def test_gate_off_and_warn_modes_and_env_override():
    for kw, expect in (({"policy": {"crossRepoGate": "off"}}, "skip"), ({"env": {"CROSS_REPO_GATE": "off"}}, "skip")):
        dp = deps(**kw)
        seed_consumer(dp["facts"])
        d = process_change(unit(), dp)
        assert cross(d)["status"] == expect and d.get("rootCauseTag") != "cross_repo_incomplete", kw
    dp = deps(policy={"crossRepoGate": "warn"})
    seed_consumer(dp["facts"])
    d = process_change(unit(), dp)
    assert d.get("rootCauseTag") != "cross_repo_incomplete" and cross(d)["status"] != "stop"
    assert "awaiting" in d["metrics"]["crossRepo"]["warning"] and FE in d["metrics"]["crossRepo"]["warning"]
    dp = deps(policy={"crossRepoGate": "warn"}, env={"CROSS_REPO_GATE": "block"})  # env wins
    seed_consumer(dp["facts"])
    assert process_change(unit(), dp)["rootCauseTag"] == "cross_repo_incomplete"


def test_router_counts_a_linked_contract_point_as_an_expensive_signal():
    pts = [{"route": "GET /api/v1/events/{id}/venue", "consumers": [FE], "missing": []}]
    r = route_change(unit(before=CTRL_V1, after=CTRL_V1 + "// c\n"), linked=pts)
    assert r["tier"] == "expensive" and f"cross-repo contract point (linked: {FE})" in r["reasons"]
    assert route_change(unit(before=CTRL_V1, after=CTRL_V1 + "// c\n"))["tier"] == "cheap"


def test_client_call_symbols_do_not_pollute_known_symbol_names():
    f = MemoryFactStore()
    seed_consumer(f)
    assert not any(n.startswith("/api") for n in f.known_symbol_names())
    assert [c["name"] for c in f.linked_clients([FE], "GET /api/v1/events/{eventId}/venue")] == ["/api/v1/events/{id}/venue"]
    assert f.linked_clients(["o/else"], "GET /api/v1/events/{id}/venue") == []
