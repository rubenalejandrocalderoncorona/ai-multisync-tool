'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { extractPublicSymbols, publicSymbols, diffPublicSymbols, splitSnapshot, docTokens } = require('../pipeline/symbols');

const names = (file, text) => extractPublicSymbols(file, text).map((s) => `${s.kind}:${s.name}`).sort();

test('JS/TS: exports are public, plain functions and locals are not', () => {
  const t = `export function createPoll(a, b) {}\nexport const MAX = 3;\nexport default function handler() {}\nfunction helper() {}\nconst local = 1;\nexport { x as y, z };\nmodule.exports = { legacy, other };`;
  assert.deepStrictEqual(names('a.ts', t), ['export:MAX', 'export:createPoll', 'export:handler', 'export:legacy', 'export:other', 'export:y', 'export:z'].sort());
});

test('routes, Next.js handlers and tRPC procedures are public interface', () => {
  assert.deepStrictEqual(names('server.js', `app.get('/health', h);\nrouter.post("/polls", h);`), ['route:GET /health', 'route:POST /polls']);
  assert.deepStrictEqual(names('apps/web/src/app/api/status/route.ts', 'export async function GET() {}\nexport const POST = async () => {}'), ['export:GET', 'export:POST', 'route:GET /api/status', 'route:POST /api/status'].sort());
  assert.deepStrictEqual(names('polls.ts', `export const pollsRouter = router({\n  create: protectedProcedure.input(x).mutation(f),\n  list: publicProcedure.query(g),\n});`), ['export:pollsRouter', 'rpc:create', 'rpc:list']);
});

test('Go exports are capitalised only; Python skips private names', () => {
  assert.deepStrictEqual(names('a.go', 'func Alert() {}\nfunc (s *Svc) Silence(id string) {}\nfunc helper() {}\ntype Config struct {}\ntype inner struct {}'), ['export:Alert', 'export:Config', 'export:Silence']);
  assert.deepStrictEqual(names('a.py', 'def public(): pass\ndef _private(): pass\nclass Thing: pass\n@app.route("/x")\ndef x(): pass'), ['export:Thing', 'export:public', 'export:x', 'route:/x'].sort());
});

test('schema models, enums and tables, env vars and config keys', () => {
  assert.deepStrictEqual(names('m.prisma', 'model User {\n id String\n}\nenum Role {\n a\n}'), ['model:Role', 'model:User']);
  assert.deepStrictEqual(names('x.sql', 'CREATE TABLE IF NOT EXISTS claims (id int);'), ['model:claims']);
  assert.deepStrictEqual(names('c.ts', 'const a = process.env.DATABASE_URL; const b = process.env.PORT;'), ['config:DATABASE_URL', 'config:PORT']);
  assert.deepStrictEqual(names('c.py', 'x = os.environ["API_KEY"]; y = os.getenv("MODE_X")'), ['config:API_KEY', 'config:MODE_X']);
});

test('diff: renaming a local variable, editing a log line or a body is NOT a public change', () => {
  const before = '### FILE: a.ts\nexport function f(a) {\n  const x = 1;\n  console.log("hi");\n  return x;\n}\n';
  const after = '### FILE: a.ts\nexport function f(a) {\n  const renamed = 1;\n  console.log("hello there");\n  return renamed;\n}\n';
  assert.strictEqual(diffPublicSymbols(before, after).touched, false);
});

test('diff: a changed signature, a new route, a removed export, a new env var and a schema field are public changes', () => {
  const base = '### FILE: a.ts\nexport function f(a) {}\nexport function g() {}\n### FILE: m.prisma\nmodel U {\n id String\n}\n';
  assert.deepStrictEqual(diffPublicSymbols(base, base.replace('f(a)', 'f(a, b)')).changed, ['export:f']);
  assert.deepStrictEqual(diffPublicSymbols(base, base + '### FILE: s.js\napp.get("/new", h);').added, ['route:GET /new']);
  assert.deepStrictEqual(diffPublicSymbols(base, base.replace('export function g() {}\n', '')).removed, ['export:g']);
  assert.deepStrictEqual(diffPublicSymbols(base, base + '### FILE: c.ts\nconst x = process.env.NEW_FLAG;').added, ['config:NEW_FLAG']);
  assert.deepStrictEqual(diffPublicSymbols(base, base.replace(' id String\n', ' id String\n email String\n')).changed, ['model:U']);
});

test('diff names are bare (without kind), unique, and the total counts the current interface', () => {
  const d = diffPublicSymbols('', '### FILE: a.ts\nexport function f() {}\nexport const f2 = 1;');
  assert.deepStrictEqual(d.names.sort(), ['f', 'f2']);
  assert.strictEqual(d.total, 2);
});

test('splitSnapshot and docTokens', () => {
  assert.deepStrictEqual(splitSnapshot('### FILE: a\nx\n### FILE: b\ny').map((f) => f.path), ['a', 'b']);
  assert.ok(docTokens('Call `createPoll` with MAX_ITEMS').has('createPoll'));
  assert.ok(!docTokens('a b').has('a'), 'tokens shorter than 3 characters are ignored');
});
