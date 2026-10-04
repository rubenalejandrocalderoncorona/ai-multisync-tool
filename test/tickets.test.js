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
const restCfg = { deskBaseUrl: 'https://tickets.example', deskToken: 't', deskProjectId: '7', deskTransport: 'rest' };

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

test('tickets: mcp is the default transport, rest is opt-in, and links use the public URL', () => {
  assert.strictEqual(createTickets({ deskBaseUrl: 'x', deskToken: 't', deskProjectId: '7', deskMcpUrl: 'http://m/api/v2/mcp' }).transport, 'mcp');
  assert.strictEqual(createTickets(restCfg).transport, 'rest');
  assert.strictEqual(createTickets({ deskBaseUrl: 'x', deskToken: '', deskProjectId: '7' }).enabled, false, 'no token, no tickets');
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

// ── MCP transport over REAL Streamable HTTP, against an in-process server with Vikunja's native tool names ──
async function startFakeMcp({ token = 'tok' } = {}) {
  const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
  const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
  const { z } = require('zod');
  const tasks = new Map(); const comments = []; const log = [];
  const build = () => {
    const s = new McpServer({ name: 'vikunja', version: 'v2.5.0' });
    const out = (v) => ({ content: [{ type: 'text', text: JSON.stringify(v) }] });
    s.registerTool('tasks_read_all', { inputSchema: { project_id: z.number().optional(), search: z.string().optional(), filter: z.string().optional(), per_page: z.number().optional() } }, async (a) => { log.push(['tasks_read_all', a]); return out([...tasks.values()].filter((t) => (!a.search || t.title.toLowerCase().includes(a.search.toLowerCase())) && !(a.filter === 'done = false' && t.done))); });
    s.registerTool('tasks_create', { inputSchema: { project_id: z.number(), title: z.string(), description: z.string().optional(), priority: z.number().optional() } }, async (a) => { log.push(['tasks_create', a]); const t = { id: tasks.size + 100, done: false, ...a }; tasks.set(t.id, t); return out(t); });
    s.registerTool('tasks_comments_create', { inputSchema: { task_id: z.number(), comment: z.string() } }, async (a) => { log.push(['tasks_comments_create', a]); comments.push(a); return out({ id: comments.length, ...a }); });
    s.registerTool('tasks_update', { inputSchema: { id: z.number(), done: z.boolean().optional(), description: z.string().optional() } }, async ({ id, ...c }) => { log.push(['tasks_update', { id, ...c }]); tasks.set(id, { ...tasks.get(id), ...c }); return out(tasks.get(id)); });
    return s;
  };
  const server = http.createServer(async (req, res) => {
    if (req.url !== '/api/v2/mcp') { res.statusCode = 404; return res.end(); }
    if (req.headers.authorization !== `Bearer ${token}`) { res.statusCode = 401; return res.end('unauthorized'); }
    const chunks = []; for await (const c of req) chunks.push(c);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
    const mcp = build(); const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on('close', () => { transport.close(); mcp.close(); });
    await mcp.connect(transport);
    await transport.handleRequest(req, res, body);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { url: `http://127.0.0.1:${server.address().port}/api/v2/mcp`, tasks, comments, log, close: () => { server.closeAllConnections?.(); server.close(); } };
}
const mcpCfg = (url, token = 'tok') => ({ deskBaseUrl: 'https://tickets.example', deskToken: token, deskProjectId: '7', deskMcpUrl: url, deskTransport: 'mcp' });

test('tickets (mcp, real Streamable HTTP): review opens, a repeat adds a real comment, approve comments and closes', async () => {
  const m = await startFakeMcp();
  try {
    const t = createTickets(mcpCfg(m.url));
    const first = await t.openReview(review);
    assert.strictEqual(first.created, true);
    assert.strictEqual(first.url, `https://tickets.example/tasks/${first.id}`);
    assert.strictEqual(m.log.find((l) => l[0] === 'tasks_create')[1].project_id, 7, 'project id is a number, as the tool requires');
    assert.match(m.tasks.get(first.id).description, /pull\/7/);

    const again = await t.openReview(review);
    assert.strictEqual(again.id, first.id, 'no duplicate task');
    assert.strictEqual(again.created, false);
    assert.match(m.comments.at(-1).comment, /Pull request updated/);

    await t.approve({ id: first.id }, { prUrl: 'https://github.com/o/docs/pull/7' });
    assert.strictEqual(m.tasks.get(first.id).done, true);
    assert.match(m.comments.at(-1).comment, /Approved for production/);
    // a closed task no longer blocks a new ticket for the same title
    assert.notStrictEqual((await t.openReview(review)).id, first.id);
  } finally { m.close(); }
});

test('tickets (mcp): the API token is sent as a Bearer header; a wrong token yields no ticket and never throws', async () => {
  const m = await startFakeMcp({ token: 'right' });
  try {
    assert.ok(await createTickets(mcpCfg(m.url, 'right')).openReview(review));
    const { escalate } = require('../pipeline/fallback');
    assert.strictEqual(await escalate({ repo: 'o/r', path: 'a.md', rootCauseTag: 'x', attempts: [] }, { ticketProvider: 'caimandesk', ...mcpCfg(m.url, 'wrong') }, async () => ({ ok: true })), null);
  } finally { m.close(); }
});

test('tickets (mcp): fallback tickets work, and an unreachable server returns null without hanging the process', async () => {
  const m = await startFakeMcp();
  try {
    assert.match(await createTickets(mcpCfg(m.url)).openFallback({ repo: 'o/r', path: 'a.md', commit: 'c1', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'x', attempts: [], feedback: ['f'], draft: '# d' }), /tasks\/\d+$/);
  } finally { m.close(); }
  const { escalate } = require('../pipeline/fallback');
  assert.strictEqual(await escalate({ repo: 'o/r', path: 'a.md', rootCauseTag: 'x', attempts: [] }, { ticketProvider: 'caimandesk', ...mcpCfg('http://127.0.0.1:9/api/v2/mcp') }, async () => ({ ok: true })), null);
});

test('ticketRefsFromBody: a promotion PR carries one marker per batch; duplicates collapse', () => {
  const body = 'Promote\n<!-- multisync:ticket=desk:42 -->\n- a\n<!-- multisync:ticket=desk:43 -->\n<!-- multisync:ticket=desk:42 -->';
  const { ticketRefsFromBody } = require('../pipeline/tickets');
  assert.deepStrictEqual(ticketRefsFromBody(body), [{ provider: 'desk', id: 42 }, { provider: 'desk', id: 43 }]);
  assert.deepStrictEqual(ticketRefsFromBody('none'), []);
});

test('tickets: QA deploy only notes the ticket (it stays open); production approval closes it', async () => {
  const { f, tasks } = restDesk([{ id: 5, title: 'x', done: false, priority: 2 }]);
  const t = createTickets(restCfg, f);
  await t.qaDeployed({ id: 5 }, { prUrl: 'https://github.com/o/docs/pull/7', siteUrl: 'https://example.org/documentation/qa/' });
  assert.strictEqual(tasks.get(5).done, false);
  await t.approve({ id: 5 }, { prUrl: 'https://github.com/o/docs/pull/8' });
  assert.strictEqual(tasks.get(5).done, true);
});
