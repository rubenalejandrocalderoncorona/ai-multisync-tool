from multisync.symbols import diff_public_symbols, doc_tokens, extract_public_symbols, split_snapshot


def names(file, text):
    return sorted(f"{s['kind']}:{s['name']}" for s in extract_public_symbols(file, text))


def test_js_ts_exports_are_public_plain_functions_and_locals_are_not():
    t = "export function createPoll(a, b) {}\nexport const MAX = 3;\nexport default function handler() {}\nfunction helper() {}\nconst local = 1;\nexport { x as y, z };\nmodule.exports = { legacy, other };"
    assert names("a.ts", t) == sorted(["export:MAX", "export:createPoll", "export:handler", "export:legacy", "export:other", "export:y", "export:z"])


def test_routes_nextjs_handlers_and_trpc_procedures_are_public_interface():
    assert names("server.js", "app.get('/health', h);\nrouter.post(\"/polls\", h);") == ["route:GET /health", "route:POST /polls"]
    assert names("apps/web/src/app/api/status/route.ts", "export async function GET() {}\nexport const POST = async () => {}") == sorted(
        ["export:GET", "export:POST", "route:GET /api/status", "route:POST /api/status"])
    assert names("polls.ts", "export const pollsRouter = router({\n  create: protectedProcedure.input(x).mutation(f),\n  list: publicProcedure.query(g),\n});") == [
        "export:pollsRouter", "rpc:create", "rpc:list"]


def test_go_exports_are_capitalised_only_python_skips_private_names():
    assert names("a.go", "func Alert() {}\nfunc (s *Svc) Silence(id string) {}\nfunc helper() {}\ntype Config struct {}\ntype inner struct {}") == ["export:Alert", "export:Config", "export:Silence"]
    assert names("a.py", 'def public(): pass\ndef _private(): pass\nclass Thing: pass\n@app.route("/x")\ndef x(): pass') == sorted(["export:Thing", "export:public", "export:x", "route:/x"])


def test_schema_models_enums_tables_env_vars_and_config_keys():
    assert names("m.prisma", "model User {\n id String\n}\nenum Role {\n a\n}") == ["model:Role", "model:User"]
    assert names("x.sql", "CREATE TABLE IF NOT EXISTS claims (id int);") == ["model:claims"]
    assert names("c.ts", "const a = process.env.DATABASE_URL; const b = process.env.PORT;") == ["config:DATABASE_URL", "config:PORT"]
    assert names("c.py", 'x = os.environ["API_KEY"]; y = os.getenv("MODE_X")') == ["config:API_KEY", "config:MODE_X"]


def test_diff_renaming_a_local_variable_editing_a_log_line_or_a_body_is_not_a_public_change():
    before = '### FILE: a.ts\nexport function f(a) {\n  const x = 1;\n  console.log("hi");\n  return x;\n}\n'
    after = '### FILE: a.ts\nexport function f(a) {\n  const renamed = 1;\n  console.log("hello there");\n  return renamed;\n}\n'
    assert diff_public_symbols(before, after)["touched"] is False


def test_diff_signature_route_removed_export_env_var_and_schema_field_are_public_changes():
    base = "### FILE: a.ts\nexport function f(a) {}\nexport function g() {}\n### FILE: m.prisma\nmodel U {\n id String\n}\n"
    assert diff_public_symbols(base, base.replace("f(a)", "f(a, b)"))["changed"] == ["export:f"]
    assert diff_public_symbols(base, base + '### FILE: s.js\napp.get("/new", h);')["added"] == ["route:GET /new"]
    assert diff_public_symbols(base, base.replace("export function g() {}\n", ""))["removed"] == ["export:g"]
    assert diff_public_symbols(base, base + "### FILE: c.ts\nconst x = process.env.NEW_FLAG;")["added"] == ["config:NEW_FLAG"]
    assert diff_public_symbols(base, base.replace(" id String\n", " id String\n email String\n"))["changed"] == ["model:U"]


def test_diff_names_are_bare_unique_and_total_counts_the_current_interface():
    d = diff_public_symbols("", "### FILE: a.ts\nexport function f() {}\nexport const f2 = 1;")
    assert sorted(d["names"]) == ["f", "f2"]
    assert d["total"] == 2


def test_split_snapshot_and_doc_tokens():
    assert [f["path"] for f in split_snapshot("### FILE: a\nx\n### FILE: b\ny")] == ["a", "b"]
    assert "createPoll" in doc_tokens("Call `createPoll` with MAX_ITEMS")
    assert "a" not in doc_tokens("a b"), "tokens shorter than 3 characters are ignored"
