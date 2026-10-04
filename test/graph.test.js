'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { processChange } = require('../pipeline/pipeline');
const { escalate } = require('../pipeline/fallback');
const { fakeLLM, makeDeps, passJudge, hallucinationJudge, DOC_V1, DOC_V2 } = require('./helpers');

const change = (over = {}) => ({ repo: 'org/svc', filePath: 'docs/api.md', commit: 'abc1234def', before: DOC_V1, after: DOC_V2, ...over });
const nodes = (d) => d.trail.map((t) => t.node);

test('graph trail: happy path visits every stage in order', async () => {
  const d = await processChange(change(), makeDeps());
  assert.deepStrictEqual(nodes(d), ['prefilter', 'cross_repo', 'similarity', 'gar', 'write_draft', 'judge', 'publish']);
  assert.ok(d.trail.every((t) => typeof t.ms === 'number' && t.runId === 'test-run' && t.at));
  assert.strictEqual(d.trail.find((t) => t.node === 'cross_repo').status, 'skip');
  assert.ok('precision' in d.trail.find((t) => t.node === 'judge').note);
});

test('graph trail: trivial diff stops after the prefilter (cheap stage only)', async () => {
  const d = await processChange(change({ after: DOC_V1 + 'x\n' }), makeDeps());
  assert.deepStrictEqual(nodes(d), ['prefilter']);
  assert.strictEqual(d.trail[0].status, 'stop');
});

test('graph trail: exhausted judge loop shows widen then fallback', async () => {
  const d = await processChange(change(), makeDeps({ llm: fakeLLM({ judges: [hallucinationJudge] }) }));
  const n = nodes(d);
  assert.ok(n.includes('widen'));
  assert.strictEqual(n[n.length - 1], 'fallback');
  assert.strictEqual(n.filter((x) => x === 'judge').length, 4); // 2 + widened budget of 2 more
  assert.strictEqual(d.trail.at(-1).status, 'fallback');
});

test('graph trail: polish loop is visible (judge -> polish_draft -> judge)', async () => {
  const llm = fakeLLM({ judges: [{ ...passJudge, quality: 0.3 }, passJudge] });
  const d = await processChange(change(), makeDeps({ llm }));
  const n = nodes(d);
  assert.deepStrictEqual(n.slice(4), ['write_draft', 'judge', 'polish_draft', 'judge', 'publish']);
});

test('logger receives one event per node, including the fallback ticket', async () => {
  const seen = [];
  const deps = makeDeps({ llm: fakeLLM({ judges: [hallucinationJudge] }) });
  deps.logger = { log: async (e) => seen.push(e) };
  deps.escalate = async () => 'https://tickets.example/tasks/9';
  const d = await processChange(change(), deps);
  assert.strictEqual(seen.length, d.trail.length);
  assert.strictEqual(d.ticket, 'https://tickets.example/tasks/9');
  assert.strictEqual(seen.at(-1).note.ticket, 'https://tickets.example/tasks/9');
});

test('a node that throws is logged as an error and the run rejects (caller turns it into pipeline_error)', async () => {
  const seen = [];
  const deps = makeDeps();
  deps.llm.embed = async () => { throw new Error('embeddings down'); };
  deps.logger = { log: async (e) => seen.push(e) };
  await assert.rejects(processChange(change(), deps), /embeddings down/);
  assert.strictEqual(seen.at(-1).status, 'error');
  assert.strictEqual(seen.at(-1).node, 'similarity');
});

// ── cAImanDesk ticketing ─────────────────────────────────────────────────────
function desk(existing = []) {
  const calls = [];
  const f = async (url, o = {}) => {
    calls.push({ url, method: o.method || 'GET', body: o.body && JSON.parse(o.body), auth: o.headers?.Authorization });
    if (url.includes('/tasks?s=')) return { ok: true, json: async () => existing };
    if (url.endsWith('/comments')) return { ok: true, json: async () => ({}) };
    return { ok: true, json: async () => ({ id: 42 }), text: async () => '' };
  };
  return { f, calls };
}
const alerts = { ticketProvider: 'caimandesk', deskTransport: 'rest', deskBaseUrl: 'https://tickets.example', deskToken: 'tok', deskProjectId: '7' };
const failure = { repo: 'org/svc', path: 'docs/a.md', commit: 'c1', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'did not converge', attempts: [{ n: 1, precision: 0.4, recall: 1, style: 1, quality: 1, failure: 'hallucinated_claim' }], feedback: ['Unsupported claim: <script>'], draft: '# d' };

test('cAImanDesk: creates a task in the configured project with bearer auth and HTML-escaped body', async () => {
  const { f, calls } = desk();
  const url = await escalate(failure, alerts, f);
  assert.strictEqual(url, 'https://tickets.example/tasks/42');
  const create = calls.find((c) => c.method === 'PUT' && c.url.endsWith('/projects/7/tasks'));
  assert.strictEqual(create.auth, 'Bearer tok');
  assert.match(create.body.title, /^\[docs-sync\] iteration_cap_exceeded: org\/svc docs\/a\.md$/);
  assert.match(create.body.description, /hallucinated_claim/);
  assert.ok(!create.body.description.includes('<script>'), 'untrusted text must be escaped');
});

test('cAImanDesk: a repeat failure comments on the open task instead of creating a duplicate', async () => {
  const title = '[docs-sync] iteration_cap_exceeded: org/svc docs/a.md';
  const { f, calls } = desk([{ id: 5, title, done: false }]);
  const url = await escalate(failure, alerts, f);
  assert.strictEqual(url, 'https://tickets.example/tasks/5');
  assert.ok(calls.some((c) => c.url.endsWith('/tasks/5/comments')));
  assert.ok(!calls.some((c) => c.method === 'PUT' && c.url.endsWith('/projects/7/tasks')));
});

test('cAImanDesk: a closed task does not suppress a new ticket', async () => {
  const title = '[docs-sync] iteration_cap_exceeded: org/svc docs/a.md';
  const { f, calls } = desk([{ id: 5, title, done: true }]);
  assert.strictEqual(await escalate(failure, alerts, f), 'https://tickets.example/tasks/42');
  assert.ok(calls.some((c) => c.method === 'PUT' && c.url.endsWith('/projects/7/tasks')));
});

test('cAImanDesk: unconfigured token or project yields no ticket and no network call', async () => {
  const { f, calls } = desk();
  assert.strictEqual(await escalate(failure, { ...alerts, deskToken: '' }, f), null);
  assert.strictEqual(calls.length, 0);
});
