'use strict';
/**
 * Contract test against the REAL cAImanDesk MCP server code (Python FastMCP), with a fake Vikunja behind it.
 * Skipped unless VIKUNJA_MCP_DIR points at the server directory (it needs its .venv). Not run in CI.
 *
 *   VIKUNJA_MCP_DIR=/path/to/vikunja-mcp npm run test:integration
 */
const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const { createTickets } = require('../../pipeline/tickets');

const DIR = process.env.VIKUNJA_MCP_DIR;
const PY = DIR && path.join(DIR, '.venv/bin/python');
const opts = { skip: DIR && fs.existsSync(PY) ? false : 'set VIKUNJA_MCP_DIR to the vikunja-mcp directory (with .venv)' };
const PORT = 8000; // the server hardcodes it

function fakeVikunja() {
  const tasks = new Map(); const seen = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const u = new URL(req.url, 'http://x'); const j = body ? JSON.parse(body) : null;
      seen.push({ method: req.method, path: u.pathname, auth: req.headers.authorization });
      const send = (o, code = 200) => { res.statusCode = code; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(o)); };
      let m;
      if ((m = u.pathname.match(/^\/api\/v1\/projects\/(\d+)\/tasks$/))) {
        if (req.method === 'GET') {
          const f = u.searchParams.get('filter') || '';
          return send([...tasks.values()].filter((t) => t.project_id === Number(m[1]) && !(f.includes('done = false') && t.done)));
        }
        if (req.method === 'PUT') { const t = { id: tasks.size + 1, done: false, assignees: null, reminders: null, project_id: Number(m[1]), ...j }; tasks.set(t.id, t); return send(t, 201); }
      }
      if ((m = u.pathname.match(/^\/api\/v1\/tasks\/(\d+)$/))) {
        const t = tasks.get(Number(m[1]));
        if (!t) return send({ message: 'not found' }, 404);
        if (req.method === 'GET') return send(t);
        if (req.method === 'POST') { Object.assign(t, j); return send(t); }
      }
      send({ message: 'unexpected' }, 404);
    });
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ server, tasks, seen, port: server.address().port })));
}

const waitPort = (port, ms = 20000) => new Promise((resolve, reject) => {
  const t0 = Date.now();
  const tryOnce = () => {
    const s = net.connect(port, '127.0.0.1');
    s.once('connect', () => { s.destroy(); resolve(); });
    s.once('error', () => { s.destroy(); Date.now() - t0 > ms ? reject(new Error('server did not start')) : setTimeout(tryOnce, 250); });
  };
  tryOnce();
});

test('REAL cAImanDesk MCP server: review ticket lifecycle through the real tools (create_task, list_tasks, get_task, update_task)', opts, async () => {
  const v = await fakeVikunja();
  const py = spawn(PY, ['mcp_server.py'], { cwd: DIR, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', VIKUNJA_URL: `http://127.0.0.1:${v.port}/api/v1`, VIKUNJA_API_TOKEN: 'test-token' }, stdio: 'ignore' });
  try {
    await waitPort(PORT);
    const t = createTickets({ deskBaseUrl: 'https://tickets.example', deskProjectId: '7', deskMcpUrl: `http://127.0.0.1:${PORT}/sse`, deskTransport: 'mcp' });
    const review = { repo: 'o/r', commit: 'abcdef1234', prUrl: 'https://github.com/o/docs/pull/7', items: [{ path: 'overview.md', precision: 1, recall: 0.9, style: 1, quality: 1 }] };

    const first = await t.openReview(review);
    assert.strictEqual(first.created, true);
    const stored = v.tasks.get(first.id);
    assert.strictEqual(stored.title, '[docs-review] o/r @ abcdef1');
    assert.strictEqual(stored.project_id, 7);
    assert.match(stored.description, /pull\/7/);
    assert.ok(v.seen.every((s) => s.auth === 'Bearer test-token'), 'every Vikunja call is authenticated by the MCP server');

    const again = await t.openReview(review);
    assert.strictEqual(again.id, first.id, 'open task with the same title is reused');
    assert.match(v.tasks.get(first.id).description, /Pull request updated/);
    assert.match(v.tasks.get(first.id).description, /overview\.md/, 'original description preserved');

    assert.strictEqual(await t.approve({ id: first.id }, { prUrl: review.prUrl }), true);
    assert.strictEqual(v.tasks.get(first.id).done, true);
    assert.match(v.tasks.get(first.id).description, /Approved\./);

    const fb = await t.openFallback({ repo: 'o/r', path: 'a.md', commit: 'c1', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'did not converge', attempts: [], feedback: ['x'], draft: '# d' });
    assert.match(fb, /tasks\/\d+$/);
  } finally {
    py.kill('SIGTERM');
    v.server.closeAllConnections?.(); v.server.close();
  }
});
