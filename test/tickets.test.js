'use strict';
const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const { createTickets, ticketRefFromBody, ticketMarker, reviewHtml } = require('../pipeline/tickets');
const { unwrap } = require('../pipeline/mcpclient');

const ITEMS = [{ path: 'overview.md', precision: 1, recall: 0.92, style: 1, quality: 0.9 }];
const review = { repo: 'o/r', commit: 'abcdef1234', prUrl: 'https://github.com/o/docs/pull/7', items: ITEMS };

// ── REST transport ────────────────────────────────────────────────────────────
function restDesk(existing = []) {
  const calls = []; const tasks = new Map(existing.map((t) => [t.id, { ...t }]));
  const f = async (url, o = {}) => {
    const method = o.method || 'GET'; const body = o.body && JSON.parse(o.body);
    calls.push({ url, method, body });
    const ok = (json) => ({ ok: true, status: 200, json: async () => json, text: async () => JSON.stringify(json) });
    if (url.includes('/tasks?s=')) return ok([...tasks.values()]);
    if (/\/projects\/\d+\/tasks$/.test(url) && method === 'PUT') { const t = { id: 42, done: false, description: '', ...body }; tasks.set(42, t); return ok(t); }
    if (/\/tasks\/\d+\/comments$/.test(url)) return ok({});
    const id = Number((url.match(/\/tasks\/(\d+)$/) || [])[1]);
    if (id && method === 'GET') return ok(tasks.get(id) || { id, title: 't', done: false });
    if (id && method === 'POST') { tasks.set(id, { ...(tasks.get(id) || {}), ...body }); return ok(body); }
    return ok({});
  };
  return { f, calls, tasks };
}
const restCfg = { deskBaseUrl: 'https://tickets.example', deskToken: 't', deskProjectId: '7' };

test('tickets (rest): openReview creates a [docs-review] task with the PR link and scores, and returns a ref', async () => {
  const { f, calls } = restDesk();
  const r = await createTickets(restCfg, f).openReview(review);
  assert.strictEqual(r.url, 'https://tickets.example/tasks/42');
  assert.strictEqual(r.ref, 'desk:42');
  const create = calls.find((c) => c.method === 'PUT' && c.url.endsWith('/projects/7/tasks'));
  assert.strictEqual(create.body.title, '[docs-review] o/r @ abcdef1');
  assert.match(create.body.description, /pull\/7/);
  assert.match(create.body.description, /overview\.md/);
  assert.match(create.body.description, /merging the pull request/);
});

test('tickets (rest): a second review for the same commit adds a note instead of a duplicate', async () => {
  const { f, calls } = restDesk([{ id: 5, title: '[docs-review] o/r @ abcdef1', done: false }]);
  const r = await createTickets(restCfg, f).openReview(review);
  assert.strictEqual(r.id, 5);
  assert.strictEqual(r.created, false);
  assert.ok(calls.some((c) => c.url.endsWith('/tasks/5/comments')));
  assert.ok(!calls.some((c) => c.method === 'PUT' && c.url.endsWith('/projects/7/tasks')));
});

test('tickets (rest): approve notes the merge and closes the task without clobbering other columns', async () => {
  const { f, calls, tasks } = restDesk([{ id: 5, title: 'x', done: false, priority: 2, description: 'keep me' }]);
  assert.strictEqual(await createTickets(restCfg, f).approve({ id: 5 }, { prUrl: 'https://github.com/o/docs/pull/7' }), true);
  assert.ok(calls.some((c) => c.url.endsWith('/tasks/5/comments') && /Approved/.test(c.body.comment)));
  assert.strictEqual(tasks.get(5).done, true);
  assert.strictEqual(tasks.get(5).priority, 2);
  assert.strictEqual(tasks.get(5).description, 'keep me');
});

test('tickets (rest): reject notes it and leaves the task OPEN', async () => {
  const { f, tasks } = restDesk([{ id: 5, title: 'x', done: false }]);
  await createTickets(restCfg, f).reject({ id: 5 }, { prUrl: 'https://github.com/o/docs/pull/7' });
  assert.strictEqual(tasks.get(5).done, false);
});

test('tickets: unconfigured means disabled and silent (no network call, null result)', async () => {
  let n = 0;
  const t = createTickets({ deskBaseUrl: 'https://x', deskProjectId: '' }, async () => { n++; });
  assert.strictEqual(t.enabled, false);
  assert.strictEqual(await t.openReview(review), null);
  assert.strictEqual(await t.openFallback({ rootCauseTag: 'x', repo: 'o/r', path: 'p' }), null);
  assert.strictEqual(await t.approve({ id: 1 }, {}), false);
  assert.strictEqual(n, 0);
});

test('tickets: transport defaults to rest with a token, to mcp with only a URL, and is overridable', () => {
  assert.strictEqual(createTickets(restCfg).transport, 'rest');
  assert.strictEqual(createTickets({ deskBaseUrl: 'x', deskProjectId: '7', deskMcpUrl: 'http://m/sse' }).transport, 'mcp');
  assert.strictEqual(createTickets({ ...restCfg, deskMcpUrl: 'http://m/sse', deskTransport: 'mcp' }).transport, 'mcp');
});

test('PR body marker round-trips and tolerates surrounding text', () => {
  const m = ticketMarker({ ref: 'desk:42' });
  assert.strictEqual(m, '<!-- multisync:ticket=desk:42 -->');
  assert.deepStrictEqual(ticketRefFromBody(`Intro\n\n${m}\n\nmore`), { provider: 'desk', id: 42 });
  assert.strictEqual(ticketRefFromBody('no marker'), null);
  assert.strictEqual(ticketRefFromBody(null), null);
});

test('review ticket body escapes untrusted text', () => {
  const h = reviewHtml({ repo: 'o/<b>r</b>', commit: 'abcdef1', prUrl: 'https://x/pull/1?a=1&b=2', items: [{ path: '<script>.md' }] });
  assert.ok(!h.includes('<script>') && !h.includes('<b>r</b>'));
  assert.match(h, /&amp;b=2/);
});

test('unwrap: structuredContent, a single JSON text, and one-text-per-element lists all normalise', () => {
  assert.deepStrictEqual(unwrap({ structuredContent: { result: [{ id: 1 }] } }), [{ id: 1 }]);
  assert.deepStrictEqual(unwrap({ structuredContent: { id: 2, title: 't' } }), { id: 2, title: 't' });
  assert.deepStrictEqual(unwrap({ content: [{ type: 'text', text: '{"id":3}' }] }), { id: 3 });
  assert.deepStrictEqual(unwrap({ content: [{ type: 'text', text: '{"id":1}' }, { type: 'text', text: '{"id":2}' }] }), [{ id: 1 }, { id: 2 }]);
  assert.strictEqual(unwrap({ content: [] }), null);
  assert.throws(() => unwrap({ isError: true, content: [{ type: 'text', text: 'boom' }] }), /MCP tool error: boom/);
});

// ── MCP transport over REAL SSE, against an in-process MCP server mimicking the cAImanDesk tool signatures ──
async function startFakeMcp() {
  const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
  const { SSEServerTransport } = require('@modelcontextprotocol/sdk/server/sse.js');
  const { z } = require('zod');
  const tasks = new Map(); const log = [];
  const build = () => {
    const s = new McpServer({ name: 'fake-vikunja-mcp', version: '1' });
    const out = (v) => ({ content: [{ type: 'text', text: JSON.stringify(v) }] });
    s.registerTool('list_tasks', { inputSchema: { project_id: z.number().optional(), filter: z.string().optional() } }, async (a) => { log.push(['list_tasks', a]); return out([...tasks.values()].filter((t) => !t.done)); });
    s.registerTool('create_task', { inputSchema: { project_id: z.number(), title: z.string(), description: z.string().optional(), priority: z.number().optional() } }, async (a) => { log.push(['create_task', a]); const t = { id: tasks.size + 100, done: false, description: '', ...a }; tasks.set(t.id, t); return out(t); });
    s.registerTool('get_task', { inputSchema: { task_id: z.number() } }, async (a) => { log.push(['get_task', a]); return out(tasks.get(a.task_id)); });
    s.registerTool('update_task', { inputSchema: { task_id: z.number(), description: z.string().optional(), done: z.boolean().optional() } }, async ({ task_id, ...c }) => { log.push(['update_task', { task_id, ...c }]); tasks.set(task_id, { ...tasks.get(task_id), ...c }); return out(tasks.get(task_id)); });
    return s;
  };
  const sessions = new Map();
  const server = http.createServer(async (req, res) => {
    if (req.method === 'GET' && req.url === '/sse') {
      const transport = new SSEServerTransport('/messages', res);
      sessions.set(transport.sessionId, transport);
      res.on('close', () => sessions.delete(transport.sessionId));
      await build().connect(transport);
    } else if (req.method === 'POST' && req.url.startsWith('/messages')) {
      const sid = new URL(req.url, 'http://x').searchParams.get('sessionId');
      await sessions.get(sid)?.handlePostMessage(req, res);
    } else { res.statusCode = 404; res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { url: `http://127.0.0.1:${server.address().port}/sse`, tasks, log, close: () => { server.closeAllConnections?.(); server.close(); } };
}
const mcpCfg = (url) => ({ deskBaseUrl: 'https://tickets.example', deskProjectId: '7', deskMcpUrl: url, deskTransport: 'mcp' });

test('tickets (mcp, real SSE): review opens, repeats note via the description, approve closes the task', async () => {
  const m = await startFakeMcp();
  try {
    const t = createTickets(mcpCfg(m.url));
    const first = await t.openReview(review);
    assert.strictEqual(first.created, true);
    assert.strictEqual(first.url, `https://tickets.example/tasks/${first.id}`);
    assert.strictEqual(m.log.find((l) => l[0] === 'create_task')[1].project_id, 7, 'project id is a number, as the tool requires');
    assert.match(m.tasks.get(first.id).description, /pull\/7/);

    const again = await t.openReview(review);
    assert.strictEqual(again.id, first.id, 'no duplicate task');
    assert.strictEqual(again.created, false);
    assert.match(m.tasks.get(first.id).description, /Pull request updated/);
    assert.match(m.tasks.get(first.id).description, /pull\/7/, 'the original description is preserved when a note is appended');

    await t.approve({ id: first.id }, { prUrl: 'https://github.com/o/docs/pull/7' });
    assert.strictEqual(m.tasks.get(first.id).done, true);
    assert.match(m.tasks.get(first.id).description, /Approved\./);
    // a closed task no longer blocks a new ticket for the same title
    const reopened = await t.openReview(review);
    assert.notStrictEqual(reopened.id, first.id);
  } finally { m.close(); }
});

test('tickets (mcp, real SSE): fallback tickets work and a down server returns null (never breaks a run)', async () => {
  const m = await startFakeMcp();
  try {
    const url = await createTickets(mcpCfg(m.url)).openFallback({ repo: 'o/r', path: 'a.md', commit: 'c1', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'x', attempts: [], feedback: ['f'], draft: '# d' });
    assert.match(url, /tasks\/\d+$/);
  } finally { m.close(); }
  const { escalate } = require('../pipeline/fallback');
  const down = await escalate({ repo: 'o/r', path: 'a.md', rootCauseTag: 'x', attempts: [] }, { ticketProvider: 'caimandesk', ...mcpCfg('http://127.0.0.1:9/sse') }, async () => ({ ok: true }));
  assert.strictEqual(down, null);
});
