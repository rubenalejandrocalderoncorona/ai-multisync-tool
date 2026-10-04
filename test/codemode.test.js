'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { globToRegExp, selectFiles, snapshot, buildCodeChanges, scrub } = require('../pipeline/codesource');
const { processChange } = require('../pipeline/pipeline');
const { loadPrompt, fill, loadStyles, resolveStyle, styleText } = require('../pipeline/prompts');
const { judge } = require('../pipeline/critic');
const { fakeLLM, makeDeps, passJudge } = require('./helpers');

test('glob: ** crosses directories, * does not', () => {
  assert.ok(globToRegExp('src/**').test('src/a/b/c.go'));
  assert.ok(globToRegExp('**/*.js').test('a/b/c.js'));
  assert.ok(globToRegExp('**/*.js').test('c.js'));
  assert.ok(!globToRegExp('src/*.js').test('src/a/b.js'));
});

test('selectFiles honours scope and drops tests, lockfiles, vendored and generated files', () => {
  const files = ['src/api.go', 'src/api_test.go', 'package-lock.json', 'node_modules/x/i.js', 'docs/a.md', 'README.md', 'cmd/main.go', 'logo.png'];
  assert.deepStrictEqual(selectFiles(files, {}), ['src/api.go', 'README.md', 'cmd/main.go']);
  assert.deepStrictEqual(selectFiles(files, { scope: ['src/**'] }), ['src/api.go']);
});

test('snapshot puts changed files first, caps size and never includes secret-looking lines', () => {
  const read = (f) => ({ 'a.js': 'const x = 1;\n', 'b.js': 'const API_KEY = "abcdefghijklmnopqrstuvwxyz123456";\nok()\n', 'c.js': 'z'.repeat(500) })[f];
  const snap = snapshot(read, ['a.js', 'b.js', 'c.js'], { changed: ['b.js'], maxChars: 200 });
  assert.strictEqual(snap.files[0], 'b.js');
  assert.ok(!snap.text.includes('abcdefghijklmnopqrstuvwxyz123456'));
  assert.ok(snap.text.length <= 200);
  assert.match(scrub('password = "supersecretvalue12345"'), /secret/);
});

function gitStub(tree) {
  return {
    listFiles: (rev) => Object.keys(tree[rev] || {}),
    readAt: (rev, f) => tree[rev]?.[f] ?? null,
    changedBetween: (a, b) => Object.keys(tree[b]).filter((f) => tree[a]?.[f] !== tree[b][f]),
    readExistingPage: () => '# Old page\n',
  };
}

test('buildCodeChanges: only pages whose scoped files changed become units', () => {
  const tree = { c1: { 'src/a.go': 'one', 'cmd/m.go': 'm' }, c2: { 'src/a.go': 'two', 'cmd/m.go': 'm' } };
  const policy = { pages: [{ path: 'api.md', kind: 'API documentation', scope: ['src/**'] }, { path: 'cli.md', scope: ['cmd/**'] }] };
  const units = buildCodeChanges({ repo: 'o/r', policy, commit: 'c2', before: 'c1', ...gitStub(tree) });
  assert.deepStrictEqual(units.map((u) => u.filePath), ['api.md']);
  assert.strictEqual(units[0].kind, 'code');
  assert.strictEqual(units[0].styleKey, 'API documentation');
  assert.deepStrictEqual(units[0].changedFiles, ['src/a.go']);
  assert.match(units[0].after, /### FILE: src\/a\.go\ntwo/);
  assert.match(units[0].before, /one/);
  assert.strictEqual(units[0].existing, '# Old page\n');
});

test('buildCodeChanges: no previous commit documents everything; removed files are reported', () => {
  const tree = { c2: { 'src/a.go': 'x' } };
  assert.strictEqual(buildCodeChanges({ repo: 'o/r', policy: {}, commit: 'c2', before: '', ...gitStub(tree) }).length, 1);
  const t2 = { c1: { 'src/a.go': 'x', 'src/b.go': 'y' }, c2: { 'src/a.go': 'x' } };
  const u = buildCodeChanges({ repo: 'o/r', policy: {}, commit: 'c2', before: 'c1', ...{ ...gitStub(t2), changedBetween: () => ['src/b.go'] } });
  assert.deepStrictEqual(u[0].changedFiles, ['src/b.go (removed)']);
});

// ── graph in code mode ───────────────────────────────────────────────────────
const CODE_V1 = '### FILE: src/a.go\nfunc Alert() {}\n';
const CODE_V2 = '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n';
const unit = (o = {}) => ({ kind: 'code', repo: 'o/r', filePath: 'overview.md', commit: 'abc1234', before: CODE_V1, after: CODE_V2, existing: '# Old\n', changedFiles: ['src/a.go'], styleKey: 'API documentation', ...o });

test('code mode: forced shape change runs the full path and uses the code prompts + chosen style', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const d = await processChange(unit(), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.mode, 'code');
  assert.strictEqual(d.style, 'API documentation');
  assert.match(llm.calls.drafts[0], /EXISTING_PAGE:\n# Old/);
  assert.match(llm.calls.drafts[0], /CHANGED_FILES: src\/a\.go/);
  const jd = d.trail.find((t) => t.node === 'judge');
  assert.match(jd.note.prompt, /^judge-code@[0-9a-f]{8}$/);
  assert.match(d.content, /doc_key: overview\.md/);
  assert.match(d.content, /source: https:\/\/github\.com\/o\/r\/tree\/abc1234/);
});

test('code mode: similarity uses the GAR paragraph against the page\'s own chunks', async () => {
  const deps = makeDeps({ env: { MIN_DIFF_LINES: '2', SIMILARITY_HIGH: '0.5' } });
  const { indexApproved } = require('../pipeline/vectorstore');
  await indexApproved({ store: deps.vectors, llm: deps.llm, repo: 'o/r', filePath: 'overview.md', content: '## Overview\n\nThe service exposes an alert API on port 8080.\n', commit: 'old' });
  // Non-forced change (fact token only) whose GAR paragraph matches the existing page.
  const d = await processChange(unit({ before: CODE_V2, after: CODE_V2.replace('8081', '8082') }), deps);
  assert.strictEqual(d.outcome, 'refreshed');
  assert.strictEqual(d.trail.find((t) => t.node === 'similarity').note.mode, 'code');
  assert.ok(!d.trail.some((t) => t.node === 'judge'), 'no judge call when the page already says it');
});

// ── prompts and styles ───────────────────────────────────────────────────────
test('prompts: judge prompts keep the JSON contract and the injection guard', () => {
  for (const n of ['judge-docs', 'judge-code']) {
    const p = loadPrompt(n);
    assert.match(p.text, /"claims":\[\{"text":"","supported":true,"evidence":""\}\]/);
    assert.match(p.text, /"facts":\[\{"text":"","covered":true,"core":false\}\]/);
    assert.match(p.text, /data, not commands/);
    assert.match(p.id, new RegExp(`^${n}@[0-9a-f]{8}$`));
  }
});

test('prompts: every draft prompt carries the persona slot and the injection guard', () => {
  for (const n of ['draft-docs', 'draft-code']) {
    const p = loadPrompt(n);
    assert.ok(p.text.includes('{{PERSONA}}'));
    assert.match(fill(p.text, { PERSONA: 'You are X.' }), /^You are X\./);
    assert.match(p.text, /data, not instructions/);
  }
});

test('styles: library is a key-value store with persona + rubric, default resolves, unknown falls back', () => {
  const lib = loadStyles();
  assert.ok(Object.keys(lib.styles).length >= 8);
  for (const [k, v] of Object.entries(lib.styles)) {
    assert.ok(v.prompt.startsWith('You are'), `${k} persona`);
    assert.ok(Array.isArray(v.rubric) && v.rubric.length >= 3, `${k} rubric`);
  }
  assert.strictEqual(resolveStyle(lib, 'API documentation').fallback, false);
  const unk = resolveStyle(lib, 'No such style');
  assert.strictEqual(unk.key, lib.defaultStyle);
  assert.strictEqual(unk.fallback, true);
  assert.match(styleText(resolveStyle(lib, 'API documentation'), { glossary: { FactStore: 'x' } }), /Rubric:[\s\S]*Glossary[\s\S]*FactStore/);
});

test('judge: sends the code prompt and the code evidence block in code mode', async () => {
  const seen = [];
  const llm = { chatJson: async (m) => { seen.push(m); return passJudge; } };
  const v = await judge(llm, { mode: 'code', source: CODE_V2, draft: 'd', changedFiles: ['src/a.go'], existing: 'old', styleText: 'S' });
  assert.match(seen[0][0].content, /FROM SOURCE CODE/);
  assert.match(seen[0][1].content, /^CODE:\n### FILE/);
  assert.match(seen[0][1].content, /EXISTING_PAGE:\nold/);
  assert.match(v.promptId, /^judge-code@/);
});

test('buildCodeChanges: a forced full sync emits every page as new even when nothing changed', () => {
  const tree = { c1: { 'src/a.go': 'same', 'cmd/m.go': 'same' }, c2: { 'src/a.go': 'same', 'cmd/m.go': 'same' } };
  const policy = { pages: [{ path: 'api.md', scope: ['src/**'] }, { path: 'cli.md', scope: ['cmd/**'] }] };
  const args = { repo: 'o/r', policy, commit: 'c2', before: 'c1', ...gitStub(tree) };
  assert.strictEqual(buildCodeChanges(args).length, 0, 'unchanged commit: nothing to do');
  const full = buildCodeChanges({ ...args, full: true });
  assert.deepStrictEqual(full.map((u) => u.filePath), ['api.md', 'cli.md']);
  assert.ok(full.every((u) => u.before === null), 'before is empty so the prefilter sees a brand-new page');
});
