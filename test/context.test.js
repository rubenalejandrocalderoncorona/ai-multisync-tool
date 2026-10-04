'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { chunkCode } = require('../pipeline/chunker');
const { syncContext, retrieveCode, retrieveSemantic } = require('../pipeline/context');
const { analyzeCode, planDocs } = require('../pipeline/stages');
const { processChange } = require('../pipeline/pipeline');
const { MemoryVectorStore } = require('../pipeline/vectorstore');
const { MemoryFactStore } = require('../pipeline/factstore');
const { judge } = require('../pipeline/critic');
const { fakeLLM, makeDeps, passJudge, hallucinationJudge, CODE_FACTS, PLAN } = require('./helpers');

// ── a tiny in-memory "git" ───────────────────────────────────────────────────
function repoOf(tree) {
  return {
    listFiles: (rev) => Object.keys(tree[rev] || {}),
    readAt: (rev, f) => tree[rev]?.[f] ?? null,
    changedBetween: (a, b) => [...new Set([...Object.keys(tree[a] || {}), ...Object.keys(tree[b] || {})])].filter((f) => tree[a]?.[f] !== tree[b]?.[f]),
  };
}
const setup = () => ({ codeStore: new MemoryVectorStore(), docStore: new MemoryVectorStore(), llm: fakeLLM({ judges: [passJudge] }), facts: new MemoryFactStore() });
const T = {
  c1: { 'src/a.go': 'package main\nfunc Alert() {}\n', 'src/b.go': 'package main\nfunc Other() {}\n', 'README.md': '# Proj\n\nAn alert service.\n', 'src/a_test.go': 'package main\nfunc TestA() {}\n', 'package-lock.json': '{}' },
  c2: { 'src/a.go': 'package main\nfunc Alert() {}\nfunc Silence() {}\n', 'src/b.go': 'package main\nfunc Other() {}\n', 'README.md': '# Proj\n\nAn alert service.\n', 'src/a_test.go': 'package main\nfunc TestA() {}\n', 'package-lock.json': '{}' },
  c3: { 'src/a.go': 'package main\nfunc Alert() {}\nfunc Silence() {}\n', 'README.md': '# Proj\n\nAn alert service.\n' },
};

test('chunkCode: windows overlap, carry their location, and cut on blank lines', () => {
  const body = Array.from({ length: 200 }, (_, i) => (i % 40 === 39 ? '' : `line ${i + 1}`)).join('\n');
  const chunks = chunkCode('src/x.go', body, { maxLines: 50, overlap: 5 });
  assert.ok(chunks.length >= 4);
  assert.match(chunks[0].text, /^FILE src\/x\.go lines 1-/);
  assert.ok(chunks[1].start <= chunks[0].end, 'chunks overlap');
  assert.ok(chunks.at(-1).end === 200);
  assert.deepStrictEqual(chunkCode('e.go', '\n\n'), []);
});

test('syncContext: first run loads the WHOLE repo into both collections, skipping tests and lockfiles', async () => {
  const s = setup();
  const r = await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s, pages: [{ path: 'overview.md', brief: 'Explain alerting.' }] });
  assert.strictEqual(r.mode, 'full');
  assert.match(r.reason, /no previous commit/);
  assert.strictEqual(r.repoFiles, 2); // a.go, b.go (README is semantic; tests and lockfile excluded)
  const paths = new Set([...s.codeStore.points.values()].map((p) => p.payload.path));
  assert.deepStrictEqual([...paths].sort(), ['src/a.go', 'src/b.go']);
  assert.ok([...s.codeStore.points.values()].every((p) => p.payload.kind === 'code' && p.payload.commit === 'c1'));
  const kinds = [...s.docStore.points.values()].map((p) => p.payload.kind).sort();
  assert.deepStrictEqual(kinds, ['brief', 'source_doc']);
  assert.strictEqual((await s.facts.getContextState('o/r')).commit, 'c1');
});

test('syncContext: when the index is at the previous commit only changed files are re-embedded', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s });
  const embedsBefore = s.llm.calls.embed;
  const r = await syncContext({ repo: 'o/r', commit: 'c2', before: 'c1', git: repoOf(T), ...s });
  assert.strictEqual(r.mode, 'incremental');
  assert.strictEqual(r.filesIndexed, 1, 'only src/a.go changed');
  assert.ok(s.llm.calls.embed - embedsBefore >= 1);
  const a = [...s.codeStore.points.values()].filter((p) => p.payload.path === 'src/a.go');
  assert.ok(a.every((p) => p.payload.commit === 'c2') && a.some((p) => p.payload.text.includes('Silence')));
  assert.ok([...s.codeStore.points.values()].filter((p) => p.payload.path === 'src/b.go').every((p) => p.payload.commit === 'c1'), 'untouched files keep their chunks');
});

test('syncContext: removed files leave the index', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c2', before: '', git: repoOf(T), ...s });
  const r = await syncContext({ repo: 'o/r', commit: 'c3', before: 'c2', git: repoOf(T), ...s });
  assert.strictEqual(r.removed, 1);
  assert.ok(![...s.codeStore.points.values()].some((p) => p.payload.path === 'src/b.go'));
});

test('syncContext: self-heals with a full re-index when the index is not at the previous commit', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s });
  // a run was missed: the index says c1 but this run claims the previous commit was c2
  const r = await syncContext({ repo: 'o/r', commit: 'c3', before: 'c2', git: repoOf(T), ...s });
  assert.strictEqual(r.mode, 'full');
  assert.match(r.reason, /index at c1/);
  assert.ok(![...s.codeStore.points.values()].some((p) => p.payload.path === 'src/b.go'), 'stale chunks are gone');
});

test('syncContext: other repos are never touched', async () => {
  const s = setup();
  await syncContext({ repo: 'o/other', commit: 'c1', before: '', git: repoOf(T), ...s });
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), full: true, ...s });
  assert.ok(await s.codeStore.count('o/other') > 0);
});

test('syncContext: secret-looking lines never reach the embedding input or the index', async () => {
  const s = setup();
  const seen = [];
  s.llm.embed = async (t) => { seen.push(...t); return t.map(() => [1, 0]); };
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf({ c1: { 'a.go': 'x := 1\nconst API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n' } }), ...s });
  assert.ok(!seen.join('\n').includes('abcdefghijklmnopqrstuvwxyz123456'));
  assert.ok(![...s.codeStore.points.values()].some((p) => p.payload.text.includes('abcdefghijklmnopqrstuvwxyz123456')));
});

test('retrieveCode: searches the whole repo index, excludes files already supplied, respects the budget', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s });
  const all = await retrieveCode({ llm: s.llm, store: s.codeStore, repo: 'o/r', queries: ['func Alert'], topK: 10 });
  assert.ok(all.length >= 1);
  const ex = await retrieveCode({ llm: s.llm, store: s.codeStore, repo: 'o/r', queries: ['func Alert'], topK: 10, exclude: ['src/a.go'] });
  assert.ok(ex.every((c) => c.path !== 'src/a.go'));
  assert.strictEqual((await retrieveCode({ llm: s.llm, store: s.codeStore, repo: 'o/r', queries: ['x'], topK: 10, budgetChars: 5 })).length, 0);
});

test('retrieveSemantic: returns approved pages, source docs and briefs, never code', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s, pages: [{ path: 'overview.md', brief: 'Explain alerting' }] });
  const hits = await retrieveSemantic({ llm: s.llm, store: s.docStore, repo: 'o/r', queries: ['alert service'], topK: 5 });
  assert.ok(hits.length >= 1 && hits.every((h) => ['approved', 'source_doc', 'brief'].includes(h.kind)));
});

// ── stage contracts ──────────────────────────────────────────────────────────
test('analyzeCode: facts without evidence are dropped; ids are kept', async () => {
  const llm = fakeLLM();
  const r = await analyzeCode(llm, { page: 'overview.md', styleKey: 'API documentation', changedFiles: ['src/a.go'], repoMap: ['src/a.go', 'src/b.go'], code: '### FILE: src/a.go\nx', related: [{ text: 'FILE src/b.go lines 1-2\ny' }] });
  assert.deepStrictEqual(r.sheet.facts.map((f) => f.id), ['F1', 'F2']);
  assert.strictEqual(r.dropped, 1);
  assert.match(llm.calls.analyzeInputs[0], /REPO_MAP:\nsrc\/a\.go\nsrc\/b\.go/);
  assert.match(llm.calls.analyzeInputs[0], /RELATED_CODE:\nFILE src\/b\.go/);
  assert.match(r.promptId, /^analyze-code@/);
});

test('planDocs: invented fact ids are removed from the plan', async () => {
  const r = await planDocs(fakeLLM(), { sheet: { facts: [{ id: 'F1' }, { id: 'F2' }] }, brief: 'b', styleText: 's', existing: '', related: [], template: '' });
  assert.deepStrictEqual(r.plan.sections[0].must_cover, ['F1', 'F2']); // F99 dropped
  assert.deepStrictEqual(r.plan.gaps, ['How silences expire']);
});

test('stage retry: malformed JSON once is recovered, twice is an error', async () => {
  let n = 0;
  const flaky = { chatJson: async () => { if (n++ === 0) throw new Error('bad json'); return CODE_FACTS; } };
  assert.ok((await analyzeCode(flaky, { page: 'p', changedFiles: [], repoMap: [], code: '', related: [] })).sheet.facts.length);
  const broken = { chatJson: async () => { throw new Error('bad json'); } };
  await assert.rejects(analyzeCode(broken, { page: 'p', changedFiles: [], repoMap: [], code: '', related: [] }), /invalid JSON twice/);
});

// ── graph: the two context stages ────────────────────────────────────────────
const CODE_V1 = '### FILE: src/a.go\nfunc Alert() {}\n';
const CODE_V2 = '### FILE: src/a.go\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n';
const unit = (o = {}) => ({ kind: 'code', repo: 'o/r', filePath: 'overview.md', commit: 'abc1234', before: CODE_V1, after: CODE_V2, existing: '# Old\n', changedFiles: ['src/a.go'], styleKey: 'API documentation', brief: 'Explain the alert API', repoMap: ['src/a.go', 'src/b.go'], snapshotFiles: ['src/a.go'], ...o });

test('graph (code mode): stage 1 code context then stage 2 semantic context, in order, before writing', async () => {
  const deps = makeDeps({ llm: fakeLLM({ judges: [passJudge] }) });
  await syncContext({ repo: 'o/r', commit: 'c0', before: '', git: repoOf({ c0: { 'src/b.go': 'package main\nfunc Other() { Alert() }\n', 'README.md': '# Proj\n\nAlert service docs.\n' } }), codeStore: deps.codeVectors, docStore: deps.vectors, llm: deps.llm, facts: deps.facts });
  const d = await processChange(unit(), deps);
  assert.deepStrictEqual(d.trail.map((t) => t.node), ['prefilter', 'cross_repo', 'route', 'similarity', 'code_context', 'gar', 'semantic_context', 'write_draft', 'verify_draft', 'judge', 'publish']);
  const cc = d.trail.find((t) => t.node === 'code_context').note;
  assert.ok(cc.related >= 1, 'pulled code from OUTSIDE the changed files');
  assert.strictEqual(cc.facts, 2);
  assert.strictEqual(cc.droppedNoEvidence, 1);
  const sc = d.trail.find((t) => t.node === 'semantic_context').note;
  assert.ok(sc.related >= 1 && sc.sections === 2 && sc.gaps === 1);
  // the writer and the judge both receive what the stages produced
  const draftIn = deps.llm.calls.drafts[0];
  assert.match(draftIn, /FACT_SHEET:\n[\s\S]*The service listens on port 8081/);
  assert.match(draftIn, /PLAN:\n[\s\S]*Configuration/);
  assert.match(draftIn, /RELATED_CODE:\nFILE src\/b\.go/);
});

test('graph (code mode): the judge sees related code, the fact sheet and the plan', async () => {
  const seen = [];
  const llm = fakeLLM({ judges: [passJudge] });
  const orig = llm.chatJson.bind(llm);
  llm.chatJson = async (m) => { if (m[0].content.includes('You are the JUDGE')) seen.push(m[1].content); return orig(m); };
  const deps = makeDeps({ llm });
  await syncContext({ repo: 'o/r', commit: 'c0', before: '', git: repoOf({ c0: { 'src/b.go': 'package main\nfunc Other() { Alert() }\n' } }), codeStore: deps.codeVectors, docStore: deps.vectors, llm, facts: deps.facts });
  await processChange(unit(), deps);
  assert.match(seen[0], /RELATED_CODE:\nFILE src\/b\.go/);
  assert.match(seen[0], /FACT_SHEET:\n[\s\S]*F1/);
  assert.match(seen[0], /PLAN:\n[\s\S]*Overview/);
});

test('graph (code mode): a widened retry re-reads the whole context with a bigger budget', async () => {
  const deps = makeDeps({ llm: fakeLLM({ judges: [hallucinationJudge] }), env: { MAX_ITERATIONS: '1' } });
  const d = await processChange(unit(), deps);
  const n = d.trail.map((t) => t.node);
  assert.deepStrictEqual(n.slice(n.indexOf('widen')), ['widen', 'code_context', 'gar', 'semantic_context', 'write_draft', 'verify_draft', 'judge', 'fallback']);
  const ccs = d.trail.filter((t) => t.node === 'code_context');
  assert.strictEqual(ccs[0].note.topK, 12);
  assert.strictEqual(ccs[1].note.topK, 30);
  assert.strictEqual(ccs[1].note.widened, true);
  assert.strictEqual(deps.llm.calls.analyze, 2);
  assert.strictEqual(deps.llm.calls.plan, 2, 'the plan is rebuilt from the larger context');
  assert.strictEqual(d.trail.filter((t) => t.node === 'semantic_context')[1].note.topK, 12);
});

test('graph (docs mode) never runs the LLM context stages (no extra cost)', async () => {
  const deps = makeDeps();
  const d = await processChange({ repo: 'o/r', filePath: 'docs/a.md', commit: 'abc', before: '## A\n\nx\n', after: '## A\n\nx\n\n- one\n- two\n- three\n' }, deps);
  assert.ok(!d.trail.some((t) => ['code_context', 'semantic_context'].includes(t.node)));
  assert.strictEqual(deps.llm.calls.analyze + deps.llm.calls.plan, 0);
});

test('syncContext: the index honors page scopes and repo-level excludes; excluded files never reach the embedding call', async () => {
  const s = setup();
  const seen = [];
  const orig = s.llm.embed.bind(s.llm);
  s.llm.embed = async (t) => { seen.push(...t); return orig(t); };
  const tree = { c1: {
    'src/a.go': 'package main\nfunc Alert() {}\n',
    'eval/fixtures/Work/notes.md': '---\nclassification: Restricted\n---\nconfidential note\n',
    'eval/fixtures/data.json': '{"confidential": true}',
    'k8s/generated.yaml': 'kind: ConfigMap # generated copy of src\n',
    'docs/readme.md': '# Docs\n\npublic\n',
  } };
  const { pagesScope } = require('../pipeline/codesource');
  const pages = [{ path: 'o.md', scope: ['src/**', 'k8s/**'] }];
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(tree), ...s, pages, scope: pagesScope(pages), exclude: ['eval/**', 'k8s/generated.yaml'] });
  const paths = [...s.codeStore.points.values()].map((p) => p.payload.path);
  assert.deepStrictEqual([...new Set(paths)], ['src/a.go']);
  assert.ok(!seen.join('\n').includes('confidential'), 'restricted fixtures were never embedded');
  assert.ok(!seen.join('\n').includes('generated copy'));
  assert.deepStrictEqual(pagesScope([{ path: 'a.md' }]), ['**'], 'a page without scope means everything');
});

test('buildCodeChanges: repo-level exclude is applied to the snapshot sent to the model', () => {
  const tree = { c1: { 'src/a.go': 'a', 'eval/x.md': 'secret notes' }, c2: { 'src/a.go': 'b', 'eval/x.md': 'secret notes 2' } };
  const { buildCodeChanges } = require('../pipeline/codesource');
  const units = buildCodeChanges({ repo: 'o/r', policy: { exclude: ['eval/**'] }, commit: 'c2', before: 'c1', ...repoOf(tree), readExistingPage: () => '' });
  assert.ok(!units[0].after.includes('secret notes'));
  assert.ok(!units[0].changedFiles.includes('eval/x.md'));
});

// ── deterministic page furniture ─────────────────────────────────────────────
const W2 = require('../pipeline/writer');
test('withChangeHistory: replaces a model-written history (invented date) with a generated, truthful one', () => {
  const draft = '## A\n\ntext\n\n## Change History\n\n| Date | Version |\n|---|---|\n| 2023-10-10 | 1.0 |\n';
  const out = W2.withChangeHistory(draft, { repo: 'o/r', commit: 'abcdef1234567', gaps: ['how silences expire'], now: new Date('2026-10-04T10:00:00Z') });
  assert.ok(!out.includes('2023-10-10'));
  assert.strictEqual((out.match(/## Change History/g) || []).length, 1);
  assert.match(out, /\| 2026-10-04 \| abcdef1 \| Documentation Bot \| Generated from o\/r at abcdef1 \| Partially filled: the source does not show how silences expire \|/);
  assert.match(W2.withChangeHistory('## A\n\nx\n', { repo: 'o/r', commit: 'abcdef1', now: new Date('2026-10-04') }), /Fully filled/);
  // sections after the history survive
  assert.match(W2.withChangeHistory('## A\n\nx\n\n## Change History\n\nold\n\n## Appendix\n\nkeep\n', { repo: 'o/r', commit: 'abcdef1' }), /## Appendix\n\nkeep[\s\S]*## Change History/);
});

test('withFrontmatter: an explicit title and description override the guessed ones', () => {
  const out = W2.withFrontmatter('## Description\n\nfirst paragraph\n', { filePath: 'overview.md', sourceUrl: 'u', commit: 'c', title: 'rurag', description: 'What rurag is.' });
  assert.match(out, /^---\ntitle: "rurag"\ndescription: "What rurag is\."/);
});

test('graph (code mode): page keeps its declared path, gets a real title and a generated change history', async () => {
  const deps = makeDeps({ policy: { trust: 'review', serviceName: 'proj', styleGuide: '', glossary: {} }, llm: fakeLLM({ judges: [passJudge], draft: '## Description\n\nA service that exposes A, B and C as its public operations, and does things well enough for a reader to use it without opening the code.\n\n## Usage\n\nCall A, B or C with the documented arguments.\n\n## Change History\n\n| 2023-10-10 | x |\n' }) });
  const d = await processChange({ kind: 'code', repo: 'o/r', filePath: 'overview.md', commit: 'abc1234', before: null, after: '### FILE: a.go\nfunc A() {}\nfunc B() {}\nfunc C() {}\n', existing: '', changedFiles: ['a.go'], brief: 'b' }, deps);
  assert.strictEqual(d.subfolder, null, 'no re-filing of a declared page');
  assert.match(d.targetPath, /services\/proj\/overview\.md$/);
  assert.match(d.content, /^---\ntitle: "proj"\n/);
  assert.match(d.content, /description: "Explain the alert|Overview/i);
  assert.ok(!d.content.includes('2023-10-10'));
  assert.match(d.content, /Documentation Bot \| Generated from o\/r at abc1234/);
});

test('judge note records which facts were missing, for the audit trail', async () => {
  const d = await processChange({ repo: 'o/r', filePath: 'docs/a.md', commit: 'abc', before: '## A\n\nx\n', after: '## A\n\nx\n\n- one\n- two\n- three\n' }, makeDeps({ llm: fakeLLM({ judges: [{ ...passJudge, facts: [{ text: 'f1', covered: true }, { text: 'the tools list', covered: true }], style: 0.9, quality: 0.9 }] }) }));
  assert.ok(d.trail.find((t) => t.node === 'judge'));
  const bad = await processChange({ repo: 'o/r', filePath: 'docs/a.md', commit: 'abc', before: '## A\n\nx\n', after: '## A\n\nx\n\n- one\n- two\n- three\n' }, makeDeps({ llm: fakeLLM({ judges: [{ claims: [{ text: 'c', supported: false }], facts: [{ text: 'the tools list', covered: false }], style: 1, quality: 1, notes: [] }] }), env: { MAX_ITERATIONS: '1' } }));
  const j = bad.trail.find((t) => t.node === 'judge').note;
  assert.deepStrictEqual(j.missing, ['the tools list']);
  assert.deepStrictEqual(j.unsupported, ['c']);
});

test('critic: a missing CORE fact fails even when the overall recall ratio is high', () => {
  const { evaluate } = require('../pipeline/critic');
  const t = makeDeps().cfg.thresholds;
  const m = { precision: 1, recall: 0.95, coreRecall: 0.5, missingCore: ['the nine MCP tools'], missing: ['the nine MCP tools'], style: 1, quality: 1, unsupported: [], notes: [] };
  const r = evaluate(m, t);
  assert.strictEqual(r.tag, 'missing_core_fact');
  assert.match(r.feedback[0], /CORE fact.*nine MCP tools/);
  assert.strictEqual(evaluate({ ...m, coreRecall: 1, missingCore: [] }, t), null);
});

test('judge: coreRecall is computed from facts flagged core', async () => {
  const llm = { chatJson: async () => ({ claims: [], facts: [{ text: 'a', covered: true, core: true }, { text: 'b', covered: false, core: true }, { text: 'c', covered: false }], style: 1, quality: 1, notes: [] }) };
  const v = await judge(llm, { mode: 'code', source: 's', draft: 'd' });
  assert.strictEqual(v.coreRecall, 0.5);
  assert.deepStrictEqual(v.missingCore, ['b']);
  assert.strictEqual(Math.round(v.recall * 100), 33);
});

// ── GAR proper: hypothetical docs from the fact sheet drive semantic retrieval ──
test('GAR (code mode): paragraphs are written from the FACT SHEET, after stage 1, and each is a query against the docs index', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const embedded = [];
  const origEmbed = llm.embed.bind(llm);
  llm.embed = async (t) => { embedded.push(...t); return origEmbed(t); };
  const d = await processChange(unit(), makeDeps({ llm }));
  const names = d.trail.map((t) => t.node);
  assert.ok(names.indexOf('code_context') < names.indexOf('gar'), 'GAR runs after the fact sheet exists');
  const gar = d.trail.find((t) => t.node === 'gar').note;
  assert.strictEqual(gar.garParagraphs, 2, 'blank paragraphs are dropped');
  assert.strictEqual(llm.calls.garFacts, 1);
  assert.ok(embedded.some((e) => e.startsWith('The service listens on a configurable port')), 'paragraph 1 embedded as a retrieval query');
  assert.ok(embedded.some((e) => e.startsWith('Alerts can be silenced by id')), 'paragraph 2 embedded as a retrieval query');
  const sc = d.trail.find((t) => t.node === 'semantic_context').note;
  assert.strictEqual(sc.garQueries, 2);
  assert.strictEqual(sc.queries, 5);
});

test('GAR: hypothetical text is never written to either index', async () => {
  const deps = makeDeps({ policy: { trust: 'auto', serviceName: 'p', styleGuide: '', glossary: {} } });
  await processChange(unit(), deps);
  const all = [...deps.vectors.points.values(), ...deps.codeVectors.points.values()].map((p) => p.payload.text).join('\n');
  assert.ok(!all.includes('configurable port and is configured through environment variables'));
});

test('GAR: a failure only degrades retrieval, it does not fail the run', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const orig = llm.chatJson.bind(llm);
  llm.chatJson = async (m) => { if (m[0].content.includes('HYPOTHETICAL documentation')) throw new Error('boom'); return orig(m); };
  const d = await processChange(unit(), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  const gar = d.trail.find((t) => t.node === 'gar').note;
  assert.strictEqual(gar.garParagraphs, 0);
  assert.strictEqual(gar.garError, 'boom');
});

test('GAR: docs mode does not run the fact-sheet GAR (no extra cost)', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const d = await processChange({ repo: 'o/r', filePath: 'docs/a.md', commit: 'abc', before: '## A\n\nx\n', after: '## A\n\nx\n\n- one\n- two\n- three\n' }, makeDeps({ llm }));
  assert.ok(!llm.calls.garFacts);
  assert.strictEqual(d.trail.find((t) => t.node === 'gar').note.garParagraphs, 0);
});

// ── semantic sources, site docs, visibility, search ──────────────────────────
const { syncSite } = require('../pipeline/context');
const { searchContext, contextStatus } = require('../scripts/context_search');
const { siteFiles } = require('../scripts/sitedocs');
const fsx = require('node:fs'); const osx = require('node:os'); const pathx = require('node:path');

test('syncContext: `docs` globs index existing documentation (outside the code scope) as semantic context; exclude still wins', async () => {
  const s = setup();
  const tree = { c1: { 'src/a.go': 'package main\nfunc A() {}\n', 'apps/docs/guide/polls.mdx': '# Polls\n\nA poll lets people vote on times.\n', 'apps/docs/secret/internal.mdx': '# Internal\n\nrestricted\n', 'CONTRIBUTING.md': '# Contributing\n\nopen a PR\n' } };
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(tree), ...s, pages: [{ path: 'o.md', scope: ['src/**'] }], scope: ['src/**'], docs: ['apps/docs/**/*.mdx', 'CONTRIBUTING.md'], exclude: ['apps/docs/secret/**'] });
  const docPaths = [...s.docStore.points.values()].map((p) => p.payload.path);
  assert.ok(docPaths.includes('apps/docs/guide/polls.mdx') && docPaths.includes('CONTRIBUTING.md'));
  assert.ok(!docPaths.some((p) => p.includes('secret')), 'excluded docs are never indexed');
  assert.deepStrictEqual([...new Set([...s.codeStore.points.values()].map((p) => p.payload.path))], ['src/a.go'], 'docs do not leak into the code collection');
});

test('syncSite: indexes the documentation site once per commit, under its own key, and skips when unchanged', async () => {
  const s = setup();
  const pages = { 'src/content/docs/guides/a.md': '## Alpha\n\nAlpha explains the alert API.\n', 'src/content/docs/guides/b.md': '## Beta\n\nBeta explains deployment.\n' };
  const args = { siteRepo: 'o/docs', commit: 's1', files: Object.keys(pages), readFile: (f) => pages[f], docStore: s.docStore, llm: s.llm, facts: s.facts };
  const first = await syncSite(args);
  assert.strictEqual(first.skipped, false);
  assert.strictEqual(first.pages, 2);
  assert.ok([...s.docStore.points.values()].every((p) => p.payload.repo === 'site:o/docs' && p.payload.kind === 'site_doc'));
  const embeds = s.llm.calls.embed;
  assert.strictEqual((await syncSite(args)).skipped, true);
  assert.strictEqual(s.llm.calls.embed, embeds, 'no re-embedding when the site commit is unchanged');
  await syncSite({ ...args, commit: 's2', files: ['src/content/docs/guides/a.md'] });
  assert.ok(![...s.docStore.points.values()].some((p) => p.payload.path.endsWith('b.md')), 'removed pages leave the index');
});

test('retrieveSemantic: with a siteRepo it also returns pages from the rest of the documentation site', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s, pages: [{ path: 'overview.md', brief: 'Explain alerting.' }] });
  await syncSite({ siteRepo: 'o/docs', commit: 's1', files: ['src/content/docs/g.md'], readFile: () => '## Alerting\n\nHow alerting works across the platform.\n', docStore: s.docStore, llm: s.llm, facts: s.facts });
  const without = await retrieveSemantic({ llm: s.llm, store: s.docStore, repo: 'o/r', queries: ['alerting platform'], topK: 5 });
  assert.ok(!without.some((h) => h.kind === 'site_doc'));
  const withSite = await retrieveSemantic({ llm: s.llm, store: s.docStore, repo: 'o/r', queries: ['alerting platform'], topK: 5, siteRepo: 'o/docs' });
  assert.ok(withSite.some((h) => h.kind === 'site_doc' && h.path === 'src/content/docs/g.md'));
});

test('siteFiles: reads Starlight pages and skips the folders the pipeline itself writes', () => {
  const dir = fsx.mkdtempSync(pathx.join(osx.tmpdir(), 'site-'));
  for (const f of ['guides/a.md', 'ci-cd/b.mdx', 'projects/x/overview.md', 'services/y/z.md', 'notes.txt']) { fsx.mkdirSync(pathx.dirname(pathx.join(dir, 'src/content/docs', f)), { recursive: true }); fsx.writeFileSync(pathx.join(dir, 'src/content/docs', f), '# x\n'); }
  assert.deepStrictEqual(siteFiles(dir), ['src/content/docs/ci-cd/b.mdx', 'src/content/docs/guides/a.md']);
  fsx.rmSync(dir, { recursive: true, force: true });
});

test('context search + status make the database inspectable', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s, pages: [{ path: 'overview.md', brief: 'Explain alerting.' }] });
  await syncSite({ siteRepo: 'o/docs', commit: 's1', files: ['p.md'], readFile: () => '## P\n\ntext\n', docStore: s.docStore, llm: s.llm, facts: s.facts });
  const r = await searchContext({ llm: s.llm, codeStore: s.codeStore, docStore: s.docStore, repo: 'o/r', query: 'func Alert', k: 3 });
  assert.ok(r.code.length >= 1 && r.code[0].path.startsWith('src/'));
  assert.ok(r.semantic.length >= 1);
  const only = await searchContext({ llm: s.llm, codeStore: s.codeStore, docStore: s.docStore, repo: 'o/r', query: 'func Alert', kind: 'code' });
  assert.strictEqual(only.semantic, undefined);
  const rows = await contextStatus({ codeStore: s.codeStore, docStore: s.docStore, facts: s.facts });
  const repoRow = rows.find((x) => x.repo === 'o/r');
  assert.strictEqual(repoRow.code, 2);
  assert.strictEqual(repoRow.source_doc >= 1, true);
  assert.strictEqual(repoRow.brief, 1);
  assert.strictEqual(rows.find((x) => x.repo === 'site:o/docs').site_doc, 1);
});

test('graph (code mode): the decision records exactly which context was retrieved and which GAR queries were used', async () => {
  const deps = makeDeps({ llm: fakeLLM({ judges: [passJudge] }) });
  await syncContext({ repo: 'o/r', commit: 'c0', before: '', git: repoOf({ c0: { 'src/b.go': 'package main\nfunc Other() { Alert() }\n', 'README.md': '# Proj\n\nAlert service docs.\n' } }), codeStore: deps.codeVectors, docStore: deps.vectors, llm: deps.llm, facts: deps.facts });
  deps.siteRepo = 'o/docs';
  const d = await processChange(unit(), deps);
  assert.ok(d.context.code.length >= 1 && d.context.code[0].path === 'src/b.go' && typeof d.context.code[0].score === 'number');
  assert.ok(d.context.semantic.length >= 1);
  assert.deepStrictEqual(d.context.facts.map((f) => f.id), ['F1', 'F2']);
  assert.strictEqual(d.context.gar.length, 2);
  assert.ok(d.context.plan.length === 2);
  assert.ok(d.trail.find((t) => t.node === 'code_context').note.relatedTop[0].startsWith('src/b.go:'));
});

// ── relevance floor, outlines, lean code-mode standards ──────────────────────
test('retrieval floor: marginal matches are dropped, but the page brief is always kept', async () => {
  const s = setup();
  await syncContext({ repo: 'o/r', commit: 'c1', before: '', git: repoOf(T), ...s, pages: [{ path: 'overview.md', brief: 'zzz qqq unrelated words' }] });
  const loose = await retrieveCode({ llm: s.llm, store: s.codeStore, repo: 'o/r', queries: ['func Alert'], topK: 10 });
  const strict = await retrieveCode({ llm: s.llm, store: s.codeStore, repo: 'o/r', queries: ['func Alert'], topK: 10, minScore: 0.999 });
  assert.ok(loose.length >= 1);
  assert.strictEqual(strict.length, 0);
  const sem = await retrieveSemantic({ llm: s.llm, store: s.docStore, repo: 'o/r', queries: ['completely different topic about bananas'], topK: 10, minScore: 0.999 });
  assert.deepStrictEqual(sem.map((x) => x.kind), ['brief'], 'only the brief survives a strict floor');
});

test('code mode writes to the style OUTLINE, skips template selection, and uses lean standards (no required References/Troubleshooting)', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const deps = makeDeps({ llm });
  const d = await processChange(unit({ styleKey: 'Data and schema reference' }), deps);
  const draftIn = llm.calls.drafts[0];
  assert.match(draftIn, /OUTLINE \(follow this order[^)]*\):\n## Overview\n## Entities/);
  assert.ok(!draftIn.includes('[Default Template]'), 'the generic business template is not used in code mode');
  assert.strictEqual(d.trail.find((t) => t.node === 'gar').note.template, 'outline:Data and schema reference');
  assert.match(llm.calls.planInputs[0], /TEMPLATE:\nOUTLINE/);
});

test('docs mode is unchanged: it still selects and applies a template', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const d = await processChange({ repo: 'o/r', filePath: 'docs/a.md', commit: 'abc', before: '## A\n\nx\n', after: '## A\n\nx\n\n- one\n- two\n- three\n' }, makeDeps({ llm }));
  assert.match(d.trail.find((t) => t.node === 'gar').note.template, /default-template/);
});

test('every bundled style has an outline, and the lean standards forbid the sections that broke the first CalendarScheduler run', () => {
  const lib = require('../pipeline/prompts').loadStyles();
  for (const [k, v] of Object.entries(lib.styles)) assert.ok(Array.isArray(v.outline) && v.outline.length >= 1, `${k} outline`);
  const std = require('../pipeline/prompts').loadPrompt('standards-code').text;
  assert.match(std, /Do not write a Change History section, a Classification section, a References section/);
});

test('judge prompts tell the model to ignore statements about the document itself', () => {
  for (const n of ['judge-docs', 'judge-code']) assert.match(require('../pipeline/prompts').loadPrompt(n).text, /Statements about the document itself/);
});

test('the decision records which files the page was based on (so a wrong scope is visible in review)', async () => {
  const deps = makeDeps({ llm: fakeLLM({ judges: [passJudge] }) });
  const d = await processChange(unit({ snapshotFiles: ['src/a.go'], repoMap: ['src/a.go', 'src/b.go', 'src/c.go'] }), deps);
  assert.deepStrictEqual(d.context.snapshot, { files: ['src/a.go'], count: 1, chars: unit().after.length, scoped: 3 });
  const note = d.trail.find((t) => t.node === 'code_context').note;
  assert.strictEqual(note.basedOnFiles, 1);
  assert.strictEqual(note.scopedFiles, 3);
});

// ── deterministic completeness ───────────────────────────────────────────────
const { checkCoverage } = require('../pipeline/coverage');
const PRISMA = 'model User {\n  id String\n}\nmodel Poll {\n  id String\n}\nenum PollStatus {\n  open\n}\nmodel Vote {\n  id String\n}\n';

test('coverage: finds every declared model/enum and reports what the page omits', () => {
  const spec = { kinds: ['prisma_model', 'prisma_enum'], min: 0.9 };
  const c = checkCoverage('### User\n\n### Poll\n\nUses `PollStatus`.', PRISMA, spec);
  assert.deepStrictEqual(c.kinds, { prisma_model: { total: 3, found: 2 }, prisma_enum: { total: 1, found: 1 } });
  // the same name matched by two extractors counts once
  assert.strictEqual(checkCoverage('User', PRISMA, { kinds: ['prisma_enum', 'graphql_type'], min: 1 }).kinds.graphql_type.total, 0);
  assert.deepStrictEqual(c.missing, ['prisma_model:Vote']);
  assert.strictEqual(c.ok, false);
  assert.strictEqual(checkCoverage('User Poll Vote PollStatus', PRISMA, spec).ok, true);
});

test('coverage: whole-word matching (a name inside another word does not count), and no declarations means nothing to enforce', () => {
  assert.strictEqual(checkCoverage('Voted and Poller', PRISMA, { kinds: ['prisma_model'], min: 1 }).kinds.prisma_model.found, 0);
  assert.strictEqual(checkCoverage('anything', 'no models here', { kinds: ['prisma_model'] }), null);
  assert.strictEqual(checkCoverage('x', PRISMA, undefined), null);
  assert.deepStrictEqual(checkCoverage('API_KEY', 'const a = process.env.API_KEY; const b = process.env.DATABASE_URL;', { kinds: ['env_var'], min: 0.5 }).missing, ['env_var:DATABASE_URL']);
});

const MANY = Array.from({ length: 10 }, (_, i) => `M${i + 1}`);
const MODELS = `### FILE: schema.prisma\n${MANY.map((m) => `model ${m} {\n  id String\n}`).join('\n')}\n`;
const schemaDraft = (names) => `## Overview\n\nThe database layer stores every entity of the application in one relational schema and this page documents them.\n\n## Entities\n\n${names.map((n) => `### ${n}\n\nThe ${n} entity.\n`).join('\n')}`;

test('graph: a schema page that names most but not all declared models passes the verifier (60%) and is caught by the coverage check (90%), with the exact names fed back', async () => {
  const llm = fakeLLM({ judges: [passJudge], draft: schemaDraft(MANY.slice(0, 7)) });
  const d = await processChange(unit({ after: MODELS, before: null, styleKey: 'Data and schema reference', changedFiles: ['schema.prisma'] }), makeDeps({ llm, env: { MAX_ITERATIONS: '2' } }));
  assert.strictEqual(d.outcome, 'fallback');
  const j = d.trail.filter((t) => t.node === 'judge');
  assert.strictEqual(j[0].note.failure, 'incomplete_coverage');
  assert.strictEqual(j[0].note.coverage.prisma_model, '7/10');
  assert.deepStrictEqual(j[0].note.coverageMissing, ['prisma_model:M8', 'prisma_model:M9', 'prisma_model:M10']);
  assert.match(llm.calls.drafts[1], /Add every one of: M8, M9, M10/, 'the next attempt is told exactly what is missing');
});

test('graph: a draft that names too few symbols never reaches the judge; the deterministic gate sends the missing names back', async () => {
  const llm = fakeLLM({ judges: [passJudge], draft: schemaDraft(MANY.slice(0, 3)) });
  const d = await processChange(unit({ after: MODELS, before: null, styleKey: 'Data and schema reference', changedFiles: ['schema.prisma'] }), makeDeps({ llm, env: { MAX_ITERATIONS: '2' } }));
  assert.strictEqual(llm.calls.judge, 0, 'no judge call was spent on an obviously incomplete draft');
  const v = d.trail.find((t) => t.node === 'verify_draft');
  assert.strictEqual(v.note.ok, false);
  assert.match(v.note.reasons[0], /Name each of them: M10, M4, M5/);
});

test('graph: when the page names every declared model, verification and coverage pass and the page publishes', async () => {
  const llm = fakeLLM({ judges: [passJudge], draft: schemaDraft(MANY) });
  const d = await processChange(unit({ after: MODELS, before: null, styleKey: 'Data and schema reference', changedFiles: ['schema.prisma'] }), makeDeps({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.trail.find((t) => t.node === 'judge').note.coverage.prisma_model, '10/10');
});
