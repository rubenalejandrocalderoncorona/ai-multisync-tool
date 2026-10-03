'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { processChange } = require('../pipeline/pipeline');
const { prefilter } = require('../pipeline/prefilter');
const { structuralChange, diffLineCount } = require('../pipeline/structure');
const { indexApproved } = require('../pipeline/vectorstore');
const { chunkMarkdown } = require('../pipeline/chunker');
const { fakeLLM, makeDeps, passJudge, hallucinationJudge, DOC_V1, DOC_V2 } = require('./helpers');

const change = (over = {}) => ({ repo: 'org/svc', filePath: 'docs/api.md', commit: 'abc1234def', before: DOC_V1, after: DOC_V2, ...over });

// ── Layer 1: zero-LLM checks ─────────────────────────────────────────────────
test('trivial diff is skipped before any network call', async () => {
  const deps = makeDeps();
  const d = await processChange(change({ after: DOC_V1 + 'x\n' }), deps);
  assert.strictEqual(d.outcome, 'skipped');
  assert.strictEqual(d.rootCauseTag, 'trivial_diff');
  assert.strictEqual(deps.llm.calls.chat + deps.llm.calls.embed + deps.llm.calls.judge, 0);
});

test('wording-only change has no structural change and costs no tokens', () => {
  const before = '## Setup\n\nRun the installer carefully.\nThen reboot the machine now.\nFinally verify it works.\n';
  const after = '## Setup\n\nExecute the installer with care.\nAfterwards restart the machine now.\nLastly confirm that it works.\n';
  const p = prefilter({ before, after, minDiffLines: 3 });
  assert.strictEqual(p.proceed, false);
  assert.strictEqual(p.tag, 'no_structural_change');
});

test('adding a list item is a forced (shape) change', () => {
  const s = structuralChange('- a\n- b\n', '- a\n- b\n- c\n');
  assert.ok(s.forced);
  assert.ok(s.reasons.includes('list_items'));
});

test('array literal growth is detected', () => {
  const s = structuralChange('const ch = ["a","b"];', 'const ch = ["a","b","c"];');
  assert.ok(s.reasons.includes('array_literals'));
});

test('diffLineCount is order-insensitive', () => {
  assert.strictEqual(diffLineCount('a\nb\nc', 'c\nb\na').total, 0);
});

// ── Layer 3: similarity short-circuit ────────────────────────────────────────
test('near-duplicate with unchanged shape only re-keys the commit hash (no generation)', async () => {
  const deps = makeDeps({ env: { MIN_DIFF_LINES: '2' } });
  await indexApproved({ store: deps.vectors, llm: deps.llm, repo: 'org/svc', filePath: 'docs/api.md', content: DOC_V2, commit: 'old0000' });
  const chatBefore = deps.llm.calls.chat;
  const d = await processChange(change({ before: DOC_V2, after: DOC_V2.replace('8080', '8081') }), deps);
  assert.strictEqual(d.outcome, 'refreshed');
  assert.strictEqual(deps.llm.calls.chat, chatBefore, 'no generation tokens spent');
  assert.strictEqual(deps.llm.calls.judge, 0);
  assert.ok([...deps.vectors.points.values()].every((p) => p.payload.commit === 'abc1234def'));
});

test('shape change overrides high similarity and forces generation', async () => {
  const deps = makeDeps();
  await indexApproved({ store: deps.vectors, llm: deps.llm, repo: 'org/svc', filePath: 'docs/api.md', content: DOC_V2, commit: 'old0000' });
  const d = await processChange(change({ before: DOC_V2, after: DOC_V2 + '- webhook\n- sms\n- teams\n' }), deps);
  assert.notStrictEqual(d.outcome, 'refreshed');
});

// ── Layer 4/5: generation, trust level ───────────────────────────────────────
test('review-trust repo yields pending_review with a Starlight page, and is NOT indexed yet', async () => {
  const deps = makeDeps();
  const d = await processChange(change(), deps);
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.reviewerAction, 'needs_review');
  assert.match(d.targetPath, /src\/content\/docs\/services\/svc\/features\/api\.md$/);
  assert.match(d.content, /^---\ntitle: /);
  assert.match(d.content, /commit: abc1234def/);
  assert.strictEqual(await deps.vectors.count(), 0, 'drafts must never be indexed before approval');
});

test('auto-trust repo yields published', async () => {
  const d = await processChange(change(), makeDeps({ policy: { trust: 'auto' } }));
  assert.strictEqual(d.outcome, 'published');
  assert.strictEqual(d.reviewerAction, 'auto_published');
});

test('hallucinated claim never converges -> fallback with attempt history, nothing to write', async () => {
  const llm = fakeLLM({ judges: [hallucinationJudge] });
  const d = await processChange(change(), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'fallback');
  assert.strictEqual(d.reviewerAction, 'auto_rejected');
  assert.strictEqual(d.rootCauseTag, 'iteration_cap_exceeded');
  assert.strictEqual(d.action, 'none');
  assert.ok(d.attempts.length >= 2);
  assert.ok(d.attempts.some((a) => a.widened), 'one automatic retry with widened top_k');
  assert.ok(d.feedback.some((f) => f.includes('supports gRPC')), 'unsupported claim is flagged for the human');
});

test('critic feedback is fed into the next draft attempt', async () => {
  const llm = fakeLLM({ judges: [hallucinationJudge, passJudge] });
  const d = await processChange(change(), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.attempts.length, 2);
  assert.match(llm.calls.drafts[1], /supports gRPC/);
});

test('grounded-but-awkward draft gets a polish-only pass and is re-scored', async () => {
  const awkward = { ...passJudge, quality: 0.4 };
  const llm = fakeLLM({ judges: [awkward, passJudge] });
  const d = await processChange(change(), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.attempts.length, 1, 'polish pass resolves it without another full attempt');
  assert.strictEqual(llm.calls.judge, 2);
});

// ── Layer 2: cross-repo gate ─────────────────────────────────────────────────
test('registered contract point with unshipped dependent is blocked before any LLM spend', async () => {
  const registry = { alert_channels_enum: { owner: 'org/svc', requires: ['org/frontend'] } };
  const deps = makeDeps({ registry });
  const d = await processChange(change({ after: DOC_V2 + '\nUses `alert_channels_enum`.\n' }), deps);
  assert.strictEqual(d.outcome, 'fallback');
  assert.strictEqual(d.rootCauseTag, 'cross_repo_incomplete');
  assert.strictEqual(deps.llm.calls.chat, 0);
});

test('cross-repo gate passes once the dependent repo has approved docs for the symbol', async () => {
  const registry = { alert_channels_enum: { owner: 'org/svc', requires: ['org/frontend'] } };
  const deps = makeDeps({ registry });
  await deps.facts.saveClaims({ repo: 'org/frontend', filePath: 'docs/ui.md', commit: 'f1', claims: [{ text: 'UI renders alert_channels_enum', supported: true }] });
  await deps.facts.approveClaims('org/frontend', 'docs/ui.md', 'f1');
  const d = await processChange(change({ after: DOC_V2 + '\nUses `alert_channels_enum`.\n' }), deps);
  assert.strictEqual(d.outcome, 'pending_review');
});

// ── Deletions, indexing, chunking ────────────────────────────────────────────
test('deleted source follows trust level without AI', async () => {
  const deps = makeDeps();
  const d = await processChange(change({ after: null }), deps);
  assert.strictEqual(d.action, 'delete');
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(deps.llm.calls.chat, 0);
});

test('indexApproved replaces a file\'s old chunks and keys them by commit', async () => {
  const deps = makeDeps();
  await indexApproved({ store: deps.vectors, llm: deps.llm, repo: 'org/svc', filePath: 'a.md', content: DOC_V1, commit: 'c1' });
  const n = await indexApproved({ store: deps.vectors, llm: deps.llm, repo: 'org/svc', filePath: 'a.md', content: DOC_V2, commit: 'c2' });
  assert.strictEqual(await deps.vectors.count('org/svc'), n);
  assert.ok([...deps.vectors.points.values()].every((p) => p.payload.commit === 'c2'));
});

test('chunker splits on headings and strips front matter', () => {
  const chunks = chunkMarkdown('---\ntitle: x\n---\n## A\n\none\n\n## B\n\ntwo\n');
  assert.deepStrictEqual(chunks.map((c) => c.heading), ['A', 'B']);
});
