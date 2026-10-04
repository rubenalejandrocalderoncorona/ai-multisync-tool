'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { routeChange } = require('../pipeline/router');
const { verifyDraft } = require('../pipeline/verify');
const { MemoryFactStore } = require('../pipeline/factstore');

const code = (before, after, over = {}) => ({ kind: 'code', repo: 'o/r', filePath: 'api.md', before, after, changedFiles: ['a.ts'], ...over });
const A = '### FILE: a.ts\nexport function createPoll(title) {\n  const x = 1;\n  console.log("created");\n  return x;\n}\n';

test('router: internals only (rename, log text, body change) -> cheap', async () => {
  const after = A.replace('const x = 1', 'const renamed = 1').replace('return x', 'return renamed').replace('"created"', '"poll created ok"');
  const r = await routeChange({ change: code(A, after) });
  assert.strictEqual(r.tier, 'cheap');
  assert.match(r.reasons[0], /internals only/);
});

test('router: a changed exported signature -> expensive, naming the symbol', async () => {
  const r = await routeChange({ change: code(A, A.replace('createPoll(title)', 'createPoll(title, options)')) });
  assert.strictEqual(r.tier, 'expensive');
  assert.match(r.reasons[0], /public interface touched \(1 changed\): createPoll/);
  assert.deepStrictEqual(r.signals.publicChanged, ['createPoll']);
});

test('router: new route, removed export, new env var, schema field -> expensive', async () => {
  for (const [label, after] of [['route', A + 'app.get("/polls", h);'], ['removed', '### FILE: a.ts\nconst x = 1;\n'], ['env', A + 'const p = process.env.POLL_LIMIT;'], ['model', A + '### FILE: m.prisma\nmodel Poll {\n id String\n}\n']]) {
    assert.strictEqual((await routeChange({ change: code(A, after) })).tier, 'expensive', label);
  }
});

test('router: a page written from scratch over public symbols -> expensive; over nothing public -> cheap', async () => {
  assert.strictEqual((await routeChange({ change: code(null, A) })).tier, 'expensive');
  assert.strictEqual((await routeChange({ change: code(null, '### FILE: a.ts\nconst x = 1;\n') })).tier, 'cheap');
});

test('router: a symbol in the cross-repo registry -> expensive even when nothing public changed', async () => {
  const r = await routeChange({ change: code(A, A + '// uses alert_channels_enum\n'), registry: { alert_channels_enum: { owner: 'o/r', requires: ['o/fe'] } } });
  assert.strictEqual(r.tier, 'expensive');
  assert.match(r.reasons.join(' '), /cross-repo contract point: alert_channels_enum/);
});

test('router: documents in OTHER repos that mention the changed symbol -> expensive; the repo\'s own docs do not count', async () => {
  const facts = new MemoryFactStore();
  const change = code(A, A.replace('createPoll(title)', 'createPoll(title, o)'));
  await facts.replaceDocRefs('o/r', 'README.md', ['createPoll'], 'source_doc'); // its own doc
  const own = await routeChange({ change, facts });
  assert.strictEqual(own.signals.referencedBy, 0);
  await facts.replaceDocRefs('o/other', 'docs/usage.md', ['createPoll'], 'source_doc');
  await facts.replaceDocRefs('site:o/docs', 'src/content/docs/g.md', ['createPoll'], 'site_doc');
  const r = await routeChange({ change, facts });
  assert.strictEqual(r.signals.referencedBy, 2);
  assert.match(r.reasons.join(' '), /referenced by 2 document\(s\) elsewhere: createPoll/);
});

test('router: docs mode defaults to cheap; ROUTER_FORCE overrides everything', async () => {
  assert.strictEqual((await routeChange({ change: { kind: 'docs', repo: 'o/r', filePath: 'docs/a.md', before: 'a', after: 'b' } })).tier, 'cheap');
  assert.strictEqual((await routeChange({ change: code(A, A), force: 'expensive' })).tier, 'expensive');
  assert.strictEqual((await routeChange({ change: code(A, A.replace('createPoll(title)', 'createPoll()')), force: 'cheap' })).tier, 'cheap');
});

// ── deterministic verification ───────────────────────────────────────────────
const GOOD = '## Overview\n\nThe createPoll function creates a poll with a title and returns its id for later use by callers.\n\n## Usage\n\nCall `createPoll` with a title string. It returns the new poll id as a string value.\n';
const ok = (over = {}) => verifyDraft({ draft: GOOD, names: ['createPoll'], filePath: 'api.md', title: 'T', description: 'D', ...over });

test('verify: a good draft passes with metrics', () => {
  const v = ok();
  assert.deepStrictEqual(v.reasons, []);
  assert.strictEqual(v.ok, true);
  assert.strictEqual(v.metrics.mentioned, '1/1');
  assert.strictEqual(v.metrics.frontMatter, 'ok');
});

test('verify: missing symbol names are listed so the next draft can fix exactly that', () => {
  const v = ok({ names: ['createPoll', 'deletePoll', 'listPolls'] });
  assert.strictEqual(v.ok, false);
  assert.match(v.reasons[0], /Name each of them: deletePoll, listPolls/);
});

test('verify: own front matter, no description source, empty, truncated and headingless drafts are all caught', () => {
  assert.match(ok({ draft: `---\ntitle: x\n---\n${GOOD}` }).reasons[0], /own front matter/);
  assert.match(ok({ title: undefined, description: undefined, draft: '## A\n\n```\ncode\n```\n\n' + 'x'.repeat(10) }).reasons.join(' '), /too short/);
  assert.ok(!ok({ draft: GOOD }).reasons.length);
  assert.match(ok({ draft: GOOD + '\n```bash\nnpm run' }).reasons.join(' '), /code fence is never closed/);
  assert.match(ok({ draft: GOOD + '\nAnd then the function continues to' }).reasons.join(' '), /mid-sentence/);
  assert.match(ok({ draft: 'Plain text with no headings at all. '.repeat(10) }).reasons.join(' '), /no section headings/);
});

test('verify: dropped content and padding are measured against the existing page', () => {
  const existing = 'x'.repeat(2000);
  assert.match(ok({ existing }).reasons.join(' '), /size of the existing page: content was dropped/);
  assert.match(ok({ existing: 'y'.repeat(420), draft: GOOD.repeat(12) }).reasons.join(' '), /size of the existing page: check for repetition/);
});

// ── FactStore coupling: populated by the context sync ────────────────────────
const { syncContext, backfillCoupling } = require('../pipeline/context');
const { MemoryVectorStore } = require('../pipeline/vectorstore');
const { fakeLLM } = require('./helpers');
const repoOf = (tree) => ({ listFiles: (r) => Object.keys(tree[r] || {}), readAt: (r, f) => tree[r]?.[f] ?? null, changedBetween: () => [] });

test('syncContext records public symbols (full, then incrementally) and which documents mention them', async () => {
  const facts = new MemoryFactStore();
  const stores = { codeStore: new MemoryVectorStore(), docStore: new MemoryVectorStore(), llm: fakeLLM(), facts };
  const tree = { c1: { 'src/a.ts': 'export function createPoll(t) {}\nfunction local() {}\n', 'src/b.ts': 'export const MAX = 3;\n', 'README.md': '# R\n\nCall createPoll with a title. MAX is the limit.\n' } };
  const r = await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(tree), ...stores });
  assert.strictEqual(r.symbols, 2);
  assert.deepStrictEqual((await facts.knownSymbolNames()).size, 2);
  assert.strictEqual(r.docRefs, 1, 'README mentions createPoll (MAX is shorter than 4 characters and is ignored as prose)');
  const info = await facts.symbolInfo('createPoll');
  assert.deepStrictEqual(info.defined, [{ repo: 'o/r', path: 'src/a.ts', kind: 'export' }]);
  assert.deepStrictEqual(info.referencedBy, [{ doc_repo: 'o/r', doc_path: 'README.md', kind: 'source_doc' }]);
  // incremental: only the changed file's symbols are replaced
  const tree2 = { c1: tree.c1, c2: { ...tree.c1, 'src/a.ts': 'export function createPoll(t, o) {}\nexport function dropPoll() {}\n' } };
  await syncContext({ repo: 'o/r', commit: 'c2', before: 'c1', git: { ...repoOf(tree2), changedBetween: () => ['src/a.ts'] }, ...stores });
  const names = [...(await facts.knownSymbolNames())].sort();
  assert.deepStrictEqual(names, ['MAX', 'createPoll', 'dropPoll']);
});

test('backfillCoupling fills symbols and doc references for an already-loaded repo, including the docs site, with no embeddings', async () => {
  const facts = new MemoryFactStore();
  const tree = { c1: { 'src/a.ts': 'export function createPoll(t) {}\n', 'docs/g.md': '# G\n\nUse createPoll.\n' } };
  const site = new Map([['src/content/docs/x.md', 'The createPoll function is documented here.']]); site.repo = 'o/docs';
  const r = await backfillCoupling({ repo: 'o/r', commit: 'c1', git: repoOf(tree), facts, siteFiles: site });
  assert.deepStrictEqual([r.symbols, r.docRefs, r.siteRefs], [1, 1, 1]);
  const refs = await facts.docRefs(['createPoll'], { excludeRepo: 'o/r' });
  assert.deepStrictEqual(refs.map((x) => x.doc_repo), ['site:o/docs'], 'the repo\'s own docs are excluded, the site counts as elsewhere');
});
