'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { planOnboarding } = require('../scripts/onboard_check');
const { loadStyles } = require('../pipeline/prompts');

const tree = (files) => ({ listFiles: () => Object.keys(files), readAt: (_r, f) => files[f] ?? null });
const styles = loadStyles();
const base = { repo: 'o/r', commit: 'abc1234', styles };

test('onboard: a sound config yields a plan with counts, an embedding estimate and no blockers', () => {
  const git = tree({ 'src/a.go': 'package main\nfunc A() {}\n', 'src/b.go': 'package main\nfunc B() {}\n', 'src/a_test.go': 'x', 'README.md': '# R\n\ntext\n', 'package-lock.json': '{}' });
  const r = planOnboarding({ ...base, git, policy: { mode: 'code', trust: 'review', glossary: { X: 'y' }, pages: [{ path: 'overview.md', kind: 'README / project overview', brief: 'b', scope: ['src/**', 'README.md'] }] } });
  assert.deepStrictEqual(r.blockers, []);
  assert.strictEqual(r.code.files, 2, 'tests and lockfiles are never indexed');
  assert.ok(r.semantic.files === 1 && r.semantic.chunks >= 2);
  assert.ok(r.embeddings.tokens > 0 && r.embeddings.usd >= 0);
  assert.strictEqual(r.pages[0].scopeFiles, 3);
});

test('onboard: a page whose scope matches nothing is a blocker', () => {
  const r = planOnboarding({ ...base, git: tree({ 'src/a.go': 'x' }), policy: { mode: 'code', pages: [{ path: 'p.md', scope: ['nope/**'], brief: 'b' }] } });
  assert.match(r.blockers.join('\n'), /scope matches NO files/);
});

test('onboard: restricted/confidential content and secret-type paths in scope are blockers; excluding them clears it', () => {
  const files = { 'src/a.go': 'x', 'notes/n.md': '---\nclassification: Oracle Restricted\n---\ntext', '.env.production': 'A=1' };
  const pol = { mode: 'code', pages: [{ path: 'p.md', scope: ['**'], brief: 'b' }] };
  const r = planOnboarding({ ...base, git: tree(files), policy: pol });
  assert.match(r.blockers.join('\n'), /look sensitive/);
  const fixed = planOnboarding({ ...base, git: tree(files), policy: { ...pol, exclude: ['notes/**', '.env*'] } });
  assert.deepStrictEqual(fixed.blockers, []);
});

test('onboard: secret-looking lines are reported (and will be scrubbed), warnings cover missing brief, glossary and auto trust', () => {
  const r = planOnboarding({ ...base, git: tree({ 'src/a.go': 'const API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\nfunc A() {}' }), policy: { mode: 'code', trust: 'auto', pages: [{ path: 'p.md', scope: ['src/**'] }] } });
  const w = r.warnings.join('\n');
  assert.match(w, /look like secrets; those lines are removed/);
  assert.match(w, /no "brief"/);
  assert.match(w, /no glossary/);
  assert.match(w, /trust is "auto"/);
});

test('onboard: coverage styles report how many declared names the page must cover; unknown styles warn', () => {
  const files = { 'prisma/m.prisma': 'model A {\n id String\n}\nmodel B {\n id String\n}\n' };
  const r = planOnboarding({ ...base, git: tree(files), policy: { mode: 'code', glossary: { x: 'y' }, pages: [{ path: 'd.md', kind: 'Data and schema reference', brief: 'b', scope: ['prisma/**'] }, { path: 'e.md', kind: 'No such style', brief: 'b', scope: ['prisma/**'] }] } });
  assert.deepStrictEqual(r.pages[0].coverage.prisma_model, 2);
  assert.match(r.warnings.join('\n'), /style "No such style" is not in config\/doc-styles.json/);
});

test('onboard: extra `docs` globs count as semantic context', () => {
  const r = planOnboarding({ ...base, git: tree({ 'src/a.go': 'x', 'apps/docs/g.mdx': '# G\n\ntext\n' }), policy: { mode: 'code', glossary: { x: 'y' }, docs: ['apps/docs/**/*.mdx'], pages: [{ path: 'p.md', scope: ['src/**'], brief: 'b' }] } });
  assert.strictEqual(r.semantic.files, 1);
});

test('onboard: ordinary source files named after credentials or using the word "restricted" are NOT flagged; real secret files and classified notes are', () => {
  const ok = planOnboarding({ ...base, git: tree({ 'src/features/credentials/data.ts': 'export const x = 1', 'src/secrets-manager.ts': 'x', 'src/poll.ts': 'const visibility = "restricted"' }), policy: { mode: 'code', glossary: { x: 'y' }, pages: [{ path: 'p.md', scope: ['src/**'], brief: 'b' }] } });
  assert.deepStrictEqual(ok.blockers, []);
  for (const f of ['config/secrets.yaml', 'deploy/secrets/db.txt', 'keys/id_rsa']) {
    const bad = planOnboarding({ ...base, git: tree({ 'src/a.go': 'x', [f]: 'x' }), policy: { mode: 'code', glossary: { x: 'y' }, pages: [{ path: 'p.md', scope: ['**'], brief: 'b' }] } });
    assert.match(bad.blockers.join('\n'), /look sensitive/, f);
  }
  // .env / .pem / .key are excluded by default, so they never reach the model and are not blockers
  const dflt = planOnboarding({ ...base, git: tree({ 'src/a.go': 'x', '.env': 'A=1', 'certs/s.pem': 'x' }), policy: { mode: 'code', glossary: { x: 'y' }, pages: [{ path: 'p.md', scope: ['**'], brief: 'b' }] } });
  assert.deepStrictEqual(dflt.blockers, []);
});
