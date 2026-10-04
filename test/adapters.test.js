'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { QdrantStore } = require('../pipeline/vectorstore');
const { escalate } = require('../pipeline/fallback');
const { evaluate } = require('../pipeline/critic');
const { LLM, parseJson } = require('../pipeline/llm');
const { loadConfig, repoPolicy } = require('../pipeline/config');
const { pointId } = require('../pipeline/chunker');

function recorder(responder) {
  const calls = [];
  const f = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : undefined, headers: opts.headers });
    const r = responder(url, opts);
    return { ok: r.ok !== false, status: r.status || 200, text: async () => JSON.stringify(r.json ?? {}), json: async () => r.json ?? {} };
  };
  return { f, calls };
}

test('Qdrant: creates collection with cosine distance + payload indexes when missing', async () => {
  const { f, calls } = recorder((url, o) => (url.endsWith('/collections/docs') && !o.method ? { ok: false, status: 404 } : { json: { result: true } }));
  const q = new QdrantStore({ url: 'http://q:6333', apiKey: 'k', collection: 'docs' }, 1536, f);
  await q.ensureCollection();
  const put = calls.find((c) => c.method === 'PUT' && c.url.endsWith('/collections/docs'));
  assert.deepStrictEqual(put.body, { vectors: { size: 1536, distance: 'Cosine' } });
  assert.strictEqual(put.headers['api-key'], 'k');
  assert.strictEqual(calls.filter((c) => c.url.endsWith('/index')).length, 4);
});

test('Qdrant: search scopes to repo and maps results', async () => {
  const { f, calls } = recorder(() => ({ json: { result: [{ id: 'a', score: 0.9, payload: { text: 't' } }] } }));
  const q = new QdrantStore({ url: 'http://q:6333', collection: 'docs' }, 3, f);
  const out = await q.search([1, 0, 0], { limit: 2, repo: 'org/svc' });
  assert.deepStrictEqual(out, [{ id: 'a', score: 0.9, payload: { text: 't' } }]);
  assert.deepStrictEqual(calls[0].body.filter, { must: [{ key: 'repo', match: { value: 'org/svc' } }] });
});

test('Qdrant: touchCommit re-keys payload without re-embedding', async () => {
  const { f, calls } = recorder(() => ({ json: {} }));
  await new QdrantStore({ url: 'http://q', collection: 'docs' }, 3, f).touchCommit(['a', 'b'], 'c9');
  assert.strictEqual(calls[0].body.payload.commit, 'c9');
  assert.deepStrictEqual(calls[0].body.points, ['a', 'b']);
});

test('pointId is deterministic and UUID-shaped', () => {
  assert.strictEqual(pointId('r', 'p', 0), pointId('r', 'p', 0));
  assert.notStrictEqual(pointId('r', 'p', 0), pointId('r', 'p', 1));
  assert.match(pointId('r', 'p', 0), /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
});

test('fallback opens a GitHub issue with the failed checks and pings Slack', async () => {
  const { f, calls } = recorder((url) => (url.includes('api.github.com') ? { json: { html_url: 'https://gh/issue/1' } } : {}));
  const ticket = await escalate(
    { repo: 'org/svc', path: 'docs/a.md', commit: 'c1', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'no converge', attempts: [{ n: 1, precision: 0.5, recall: 1, style: 1, quality: 1, failure: 'hallucinated_claim' }], feedback: ['Unsupported claim: x'], draft: '# d' },
    { ticketProvider: 'github', githubToken: 't', githubRepo: 'org/central', slackWebhook: 'https://hooks/slack' }, f);
  assert.strictEqual(ticket, 'https://gh/issue/1');
  const issue = calls.find((c) => c.url.includes('/repos/org/central/issues'));
  assert.match(issue.body.body, /iteration_cap_exceeded/);
  assert.match(issue.body.body, /Unsupported claim: x/);
  assert.ok(calls.some((c) => c.url === 'https://hooks/slack'));
});

test('fallback never throws when delivery fails', async () => {
  const f = async () => { throw new Error('network down'); };
  const t = await escalate({ repo: 'o/r', path: 'p', rootCauseTag: 'x', attempts: [] }, { ticketProvider: 'github', githubToken: 't', githubRepo: 'o/c', slackWebhook: 'https://h' }, f);
  assert.strictEqual(t, null);
});

test('critic: hallucination outranks every other failure', () => {
  const t = loadConfig({}).thresholds;
  const r = evaluate({ precision: 0.5, recall: 0.1, style: 0.1, quality: 0.1, unsupported: ['bad'], missing: ['m'], notes: [] }, t);
  assert.strictEqual(r.tag, 'hallucinated_claim');
  assert.match(r.feedback[0], /bad/);
});

test('critic: passes only when every metric clears its threshold', () => {
  const t = loadConfig({}).thresholds;
  assert.strictEqual(evaluate({ precision: 1, recall: 1, style: 0.9, quality: 0.9, unsupported: [], missing: [], notes: [] }, t), null);
  assert.strictEqual(evaluate({ precision: 1, recall: 0.5, style: 0.9, quality: 0.9, unsupported: [], missing: ['f'], notes: [] }, t).tag, 'missing_claim');
});

test('LLM client: sends bearer auth, parses fenced JSON, orders embeddings by index', async () => {
  let seen;
  const f = async (url, opts) => {
    seen = { url, opts };
    const body = JSON.parse(opts.body);
    const json = url.endsWith('/embeddings')
      ? { data: [{ index: 1, embedding: [2] }, { index: 0, embedding: [1] }] }
      : { choices: [{ message: { content: '```json\n{"a":1}\n```' } }] };
    return { ok: true, text: async () => JSON.stringify(json), _body: body };
  };
  const llm = new LLM({ baseUrl: 'http://x', chatPath: '/v1/chat/completions', embedPath: '/v1/embeddings', apiKey: 'sk', model: 'm', fastModel: 'fm', embedModel: 'e', timeoutMs: 1000 }, f);
  assert.deepStrictEqual(await llm.chatJson([{ role: 'user', content: 'hi' }]), { a: 1 });
  assert.strictEqual(seen.opts.headers.Authorization, 'Bearer sk');
  assert.deepStrictEqual(await llm.embed(['a', 'b']), [[1], [2]]);
  assert.deepStrictEqual(parseJson('noise {"z":2} tail'), { z: 2 });
});

test('repoPolicy: unknown repos default to review (never auto-publish)', () => {
  const p = repoPolicy({ defaults: { trust: 'review' }, repos: { 'o/trusted': { trust: 'auto' } } }, 'o/unknown');
  assert.strictEqual(p.trust, 'review');
  assert.strictEqual(repoPolicy({ defaults: {}, repos: { 'o/trusted': { trust: 'auto' } } }, 'o/trusted').trust, 'auto');
});

// ── LLM retries ──────────────────────────────────────────────────────────────
const { retryDelayMs } = require('../pipeline/llm');
const cfgAi = { baseUrl: 'http://x', chatPath: '/c', embedPath: '/e', apiKey: 'k', model: 'm', fastModel: 'f', embedModel: 'e', timeoutMs: 1000, maxRetries: 3 };
const resp = (status, body, headers = {}) => ({ ok: status < 300, status, headers: { get: (k) => headers[k.toLowerCase()] }, text: async () => (typeof body === 'string' ? body : JSON.stringify(body)) });

test('LLM retry: a 429 with "try again in 25.9s" waits that long (plus margin) then succeeds', async () => {
  const waits = []; let n = 0;
  const f = async () => (n++ < 2 ? resp(429, { error: { message: 'Rate limit reached. Please try again in 25.888s.' } }) : resp(200, { choices: [{ message: { content: 'ok' } }] }));
  const llm = new LLM(cfgAi, f, async (ms) => waits.push(ms));
  assert.strictEqual(await llm.chat([{ role: 'user', content: 'x' }]), 'ok');
  assert.strictEqual(n, 3);
  assert.deepStrictEqual(waits, [26638, 26638]);
});

test('LLM retry: honors Retry-After, backs off exponentially without a hint, and caps the wait', () => {
  assert.strictEqual(retryDelayMs(resp(429, '', { 'retry-after': '7' }), '', 0), 7000);
  assert.strictEqual(retryDelayMs(resp(500, ''), 'oops', 0), 1000);
  assert.strictEqual(retryDelayMs(resp(500, ''), 'oops', 3), 8000);
  assert.strictEqual(retryDelayMs(resp(500, ''), 'oops', 10), 30000);
  assert.strictEqual(retryDelayMs(resp(429, ''), 'try again in 800ms', 0), 1550);
  assert.strictEqual(retryDelayMs(resp(429, '', { 'retry-after': '9999' }), '', 0), 90000);
});

test('LLM retry: 5xx and network errors are retried; 400/401 are not; gives up after maxRetries', async () => {
  let n = 0;
  const flaky = async () => { n++; if (n === 1) throw new Error('fetch failed'); if (n === 2) return resp(503, 'unavailable'); return resp(200, { data: [{ index: 0, embedding: [1] }] }); };
  assert.deepStrictEqual(await new LLM(cfgAi, flaky, async () => {}).embed(['a']), [[1]]);
  assert.strictEqual(n, 3);

  let bad = 0;
  await assert.rejects(new LLM(cfgAi, async () => { bad++; return resp(401, 'nope'); }, async () => {}).chat([{ role: 'user', content: 'x' }]), /AI API 401/);
  assert.strictEqual(bad, 1, 'a 401 is never retried');

  let always = 0;
  await assert.rejects(new LLM(cfgAi, async () => { always++; return resp(429, 'slow down'); }, async () => {}).chat([{ role: 'user', content: 'x' }]), /AI API 429/);
  assert.strictEqual(always, 4, '1 try + 3 retries');
});
