'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { processChange } = require('../pipeline/pipeline');
const { LLM } = require('../pipeline/llm');
const { loadConfig } = require('../pipeline/config');
const { fakeLLM, makeDeps, passJudge, GOOD_DRAFT } = require('./helpers');

const mk = (o = {}) => makeDeps({ ...o, env: { MIN_DIFF_LINES: '1', ...(o.env || {}) } });
const SNAP = (body) => `### FILE: src/a.go\npackage main\n${body}`;
const V1 = SNAP('func Alert() {\n  x := 1\n  log("a")\n}\nfunc Silence(id string) {}\n');
const unit = (before, after, o = {}) => ({ kind: 'code', repo: 'o/r', filePath: 'overview.md', commit: 'abc1234', before, after, existing: '# Old\n\nOld text that is long enough to count as an existing page for comparison.\n', changedFiles: ['src/a.go'], styleKey: 'API documentation', brief: 'b', repoMap: ['src/a.go'], snapshotFiles: ['src/a.go'], ...o });
// An internal change that survives the zero-cost prefilter: a new PRIVATE helper (lowercase, not exported) and a changed number.
const INTERNAL = (v) => `${v}func helper() {\n  retries := 3\n}\n`;
const tiersOf = (llm, kind) => llm.calls.log.filter((l) => l.kind === kind).map((l) => l.tier);

// ── routing by stage ─────────────────────────────────────────────────────────
test('INTERNALS ONLY: analysis, plan and draft run on the CHEAP tier; GAR is cheap; the judge stays expensive', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const d = await processChange(unit(V1, INTERNAL(V1)), mk({ llm }));
  assert.strictEqual(d.tier, 'cheap');
  assert.strictEqual(d.trail.find((t) => t.node === 'route').note.tier, 'cheap');
  assert.deepStrictEqual(tiersOf(llm, 'analyze'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'plan'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'draft'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'gar'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'gar-facts'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'judge'), ['expensive']);
});

test('PUBLIC INTERFACE TOUCHED: analysis, plan and draft run on the EXPENSIVE tier, GAR still cheap', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const after = V1.replace('func Silence(id string)', 'func Silence(id string, reason string)');
  const d = await processChange(unit(V1, after), mk({ llm }));
  assert.strictEqual(d.tier, 'expensive');
  assert.match(d.route.reasons[0], /public interface touched \(1 changed\): Silence/);
  assert.deepStrictEqual(tiersOf(llm, 'analyze'), ['expensive']);
  assert.deepStrictEqual(tiersOf(llm, 'draft'), ['expensive']);
  assert.deepStrictEqual(tiersOf(llm, 'gar'), ['cheap']);
  assert.deepStrictEqual(tiersOf(llm, 'gar-facts'), ['cheap']);
});

test('RETRIEVAL IS NOT AN LLM CALL: embeddings only, no chat, in the similarity stage', async () => {
  const llm = fakeLLM({ judges: [passJudge] });
  const before = llm.calls.chat;
  const { MemoryVectorStore } = require('../pipeline/vectorstore');
  const deps = mk({ llm, vectors: new MemoryVectorStore() });
  const d = await processChange(unit(V1, INTERNAL(V1)), deps);
  const sim = d.trail.find((t) => t.node === 'similarity');
  assert.ok(sim && sim.note.minChunkSimilarity !== undefined);
  assert.ok(!sim.note.usage?.cheap && !sim.note.usage?.expensive, 'the similarity stage spent no model tokens');
  assert.ok(before === 0);
});

test('ROUTER_FORCE and ROUTER_JUDGE override the defaults', async () => {
  const a = fakeLLM({ judges: [passJudge] });
  await processChange(unit(V1, V1.replace('func Silence(id string)', 'func Silence()')), mk({ llm: a, env: { ROUTER_FORCE: 'cheap' } }));
  assert.deepStrictEqual(tiersOf(a, 'draft'), ['cheap']);
  const b = fakeLLM({ judges: [passJudge] });
  await processChange(unit(V1, INTERNAL(V1)), mk({ llm: b, env: { ROUTER_JUDGE: 'follow' } }));
  assert.deepStrictEqual(tiersOf(b, 'judge'), ['cheap'], 'follow = the judge uses the draft tier');
  const c = fakeLLM({ judges: [passJudge] });
  await processChange(unit(V1, INTERNAL(V1)), mk({ llm: c, env: { ROUTER_JUDGE: 'cheap' } }));
  assert.deepStrictEqual(tiersOf(c, 'judge'), ['cheap']);
});

// ── escalation ───────────────────────────────────────────────────────────────
const SHORT = '## Overview\n\nToo short.';

test('ESCALATION: a cheap draft that fails the deterministic check is redone once on the expensive tier, without using an attempt or a judge call', async () => {
  const llm = fakeLLM({ judges: [passJudge], draft: (m, o) => ((o?.tier === 'cheap') ? SHORT : GOOD_DRAFT) });
  const d = await processChange(unit(V1, INTERNAL(V1)), mk({ llm }));
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.escalated, true);
  assert.strictEqual(d.tier, 'expensive');
  assert.deepStrictEqual(tiersOf(llm, 'draft'), ['cheap', 'expensive']);
  const names = d.trail.map((t) => t.node);
  assert.deepStrictEqual(names.slice(names.indexOf('write_draft')), ['write_draft', 'verify_draft', 'write_draft', 'verify_draft', 'judge', 'publish']);
  assert.strictEqual(d.trail.filter((t) => t.node === 'verify_draft')[0].status, 'escalate');
  assert.strictEqual(d.attempts.length, 1, 'the escalated redo is not counted as an attempt');
  assert.strictEqual(llm.calls.judge, 1, 'the weak draft never cost a judge call');
  assert.match(llm.calls.drafts[1], /Fix exactly these problems:[\s\S]*too short/);
});

test('ESCALATION happens at most once: a failing expensive draft goes back to the writer, not up again', async () => {
  const llm = fakeLLM({ judges: [passJudge], draft: SHORT });
  const d = await processChange(unit(V1, INTERNAL(V1)), mk({ llm, env: { MAX_ITERATIONS: '2' } }));
  assert.strictEqual(d.outcome, 'fallback');
  assert.strictEqual(llm.calls.judge, 0);
  assert.deepStrictEqual(tiersOf(llm, 'draft').slice(0, 2), ['cheap', 'expensive']);
  assert.ok(tiersOf(llm, 'draft').slice(1).every((t) => t === 'expensive'));
});

test('an expensive-tier draft is never "escalated"; its failures are feedback for the next attempt', async () => {
  let n = 0;
  const llm = fakeLLM({ judges: [passJudge], draft: () => (n++ === 0 ? SHORT : GOOD_DRAFT) });
  const d = await processChange(unit(V1, V1.replace('func Silence(id string)', 'func Silence()')), mk({ llm }));
  assert.strictEqual(d.escalated, false);
  assert.strictEqual(d.outcome, 'pending_review');
  assert.strictEqual(d.attempts[0].failure, 'deterministic_check');
});

// ── the client: tiers, endpoints, parameters, cost ───────────────────────────
function recordingFetch() {
  const calls = [];
  const f = async (url, o) => {
    const body = JSON.parse(o.body);
    calls.push({ url, auth: o.headers.Authorization, body });
    const content = url.endsWith('/embeddings') ? undefined : '{"ok":true}';
    const json = url.endsWith('/embeddings') ? { data: [{ index: 0, embedding: [1] }], usage: { total_tokens: 5 } } : { choices: [{ message: { content } }], usage: { prompt_tokens: 1000, completion_tokens: 500 } };
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(json) };
  };
  return { f, calls };
}
const cfg = loadConfig({ INTERNAL_AI_API_KEY: 'sk-openai', DEEPSEEK_API_KEY: 'sk-deepseek' }).ai;

test('client: the cheap tier goes to DeepSeek with its own key and model; the expensive tier to OpenAI', async () => {
  const { f, calls } = recordingFetch();
  const llm = new LLM(cfg, f);
  await llm.chat([{ role: 'user', content: 'x' }], { tier: 'cheap' });
  await llm.chat([{ role: 'user', content: 'x' }], { fast: true });
  await llm.chat([{ role: 'user', content: 'x' }]);
  assert.strictEqual(calls[0].url, 'https://api.deepseek.com/chat/completions');
  assert.strictEqual(calls[0].auth, 'Bearer sk-deepseek');
  assert.strictEqual(calls[0].body.model, 'deepseek-v4-pro');
  assert.strictEqual(calls[1].url, calls[0].url, '`fast: true` is the cheap tier');
  assert.strictEqual(calls[2].url, 'https://api.openai.com/v1/chat/completions');
  assert.strictEqual(calls[2].auth, 'Bearer sk-openai');
  assert.strictEqual(calls[2].body.model, 'gpt-5.6-terra');
});

test('client: temperature 0 for DeepSeek, omitted for gpt-5.x (which rejects it), and embeddings always go to OpenAI', async () => {
  const { f, calls } = recordingFetch();
  const llm = new LLM(cfg, f);
  await llm.chat([{ role: 'user', content: 'x' }], { tier: 'cheap' });
  await llm.chat([{ role: 'user', content: 'x' }], { tier: 'expensive' });
  await llm.embed(['text']);
  assert.strictEqual(calls[0].body.temperature, 0);
  assert.ok(!('temperature' in calls[1].body));
  assert.strictEqual(calls[2].url, 'https://api.openai.com/v1/embeddings');
  assert.strictEqual(calls[2].auth, 'Bearer sk-openai');
});

test('client: without a DeepSeek key the cheap tier falls back to a small model on the primary provider', () => {
  const c = loadConfig({ INTERNAL_AI_API_KEY: 'sk-openai' }).ai;
  assert.strictEqual(c.tiers.cheap.baseUrl, 'https://api.openai.com');
  assert.strictEqual(c.tiers.cheap.model, 'gpt-4o-mini');
});

test('client: usage and an estimated cost are tracked per tier, and each stage records its own delta', async () => {
  const { f } = recordingFetch();
  const llm = new LLM(cfg, f);
  const before = llm.snapshotUsage();
  await llm.chat([{ role: 'user', content: 'x' }], { tier: 'cheap' });
  await llm.chat([{ role: 'user', content: 'x' }], { tier: 'expensive' });
  const u = llm.snapshotUsage();
  assert.strictEqual(u.cheap.calls, 1);
  assert.strictEqual(u.cheap.in, 1000);
  assert.ok(Math.abs(u.cheap.usd - (1000 * 0.66 + 500 * 1.98) / 1e6) < 1e-9, 'DeepSeek pro estimate');
  assert.ok(Math.abs(u.expensive.usd - (1000 * 2 + 500 * 12) / 1e6) < 1e-9, 'terra estimate');
  assert.strictEqual(before.cheap.calls, 0, 'a snapshot is a copy, not a live view');
});

test('graph: the decision carries a cost summary split by tier, and node notes carry their own usage', async () => {
  const { f } = recordingFetch();
  const real = new LLM(cfg, f);
  // wrap: scripted answers from the fake, token accounting from the real client
  const fake = fakeLLM({ judges: [passJudge] });
  const llm = {
    ...fake, usage: real.usage, snapshotUsage: () => real.snapshotUsage(), tiers: real.tiers,
    chat: async (m, o) => { await real.chat(m, o); return fake.chat(m, o); },
    chatJson: async (m, o) => { await real.chat(m, o); return fake.chatJson(m, o); },
  };
  const d = await processChange(unit(V1, INTERNAL(V1)), mk({ llm }));
  assert.ok(d.cost.cheap.calls >= 3 && d.cost.expensive.calls >= 1);
  assert.ok(d.cost.usd > 0);
  assert.ok(d.trail.some((t) => t.note.usage?.cheap));
  assert.strictEqual(d.cost.usd, Number((d.cost.cheap.usd + d.cost.expensive.usd).toFixed(4)));
});
