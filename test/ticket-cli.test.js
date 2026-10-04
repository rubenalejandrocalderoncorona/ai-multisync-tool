'use strict';
const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const SCRIPT = path.join(__dirname, '../scripts/review_ticket.js');
const run = (args, o) => new Promise((resolve) => {
  const p = spawn('node', [SCRIPT, ...args], o); let out = ''; let err = '';
  p.stdout.on('data', (d) => (out += d)); p.stderr.on('data', (d) => (err += d));
  p.on('close', (code) => resolve({ code, out: out.trim(), err }));
});

function fakeVikunjaRest() {
  const tasks = new Map();
  const server = http.createServer((req, res) => {
    let b = ''; req.on('data', (c) => (b += c));
    req.on('end', () => {
      const u = new URL(req.url, 'http://x'); const j = b ? JSON.parse(b) : null;
      const send = (o) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(o)); };
      if (req.headers.authorization !== 'Bearer tok') { res.statusCode = 401; return res.end('{}'); }
      let m;
      if (/\/projects\/\d+\/tasks$/.test(u.pathname)) {
        if (req.method === 'GET') return send([...tasks.values()]);
        const t = { id: tasks.size + 1, done: false, comments: [], ...j }; tasks.set(t.id, t); return send(t);
      }
      if ((m = u.pathname.match(/\/tasks\/(\d+)\/comments$/))) { const t = tasks.get(Number(m[1])); if (!t) { res.statusCode = 404; return res.end('{}'); } t.comments.push(j.comment); return send({}); }
      if ((m = u.pathname.match(/\/tasks\/(\d+)$/))) { const t = tasks.get(Number(m[1])); if (!t) { res.statusCode = 404; return res.end('{}'); } if (req.method === 'POST') Object.assign(t, j); return send(t); }
      res.statusCode = 404; res.end('{}');
    });
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ server, tasks, url: `http://127.0.0.1:${server.address().port}` })));
}

test('review ticket CLI: open -> marker; approve closes the task; reject leaves it open; missing marker and dead server never fail the step', async () => {
  const v = await fakeVikunjaRest();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rt-'));
  fs.writeFileSync(path.join(dir, 'pipeline-results.json'), JSON.stringify({
    repo: 'cAImanLabs/cAImanLabsCalendarScheduler', commit: 'abcdef1234567',
    results: [{ path: 'overview.md', outcome: 'pending_review', metrics: { final: { precision: 1, recall: 0.9, style: 1, quality: 1 } } }, { path: 'x.md', outcome: 'skipped' }],
  }));
  const base = { ...process.env, CAIMANDESK_URL: v.url, CAIMANDESK_API_TOKEN: 'tok', CAIMANDESK_PROJECT_ID: '7', CAIMANDESK_TRANSPORT: 'rest' };
  try {
    const opened = await run(['open'], { cwd: dir, env: { ...base, PR_URL: 'https://github.com/o/docs/pull/9' } });
    assert.strictEqual(opened.code, 0, opened.err);
    const o = JSON.parse(opened.out.split('\n').pop());
    assert.strictEqual(o.marker, `<!-- multisync:ticket=desk:${o.id} -->`);
    assert.strictEqual(o.url, `${v.url}/tasks/${o.id}`);
    const t = v.tasks.get(o.id);
    assert.strictEqual(t.title, '[docs-review] cAImanLabs/cAImanLabsCalendarScheduler @ abcdef1');
    assert.match(t.description, /overview\.md/);
    assert.ok(!t.description.includes('x.md'), 'only pages awaiting review are listed');

    const rejected = await run(['reject'], { cwd: dir, env: { ...base, PR_URL: 'https://github.com/o/docs/pull/9', PR_BODY: `intro\n${o.marker}` } });
    assert.strictEqual(JSON.parse(rejected.out).ok, true);
    assert.strictEqual(t.done, false, 'closed-unmerged keeps the ticket open');
    assert.match(t.comments.at(-1), /Not approved/);

    // QA deploy: noted, still open; one promotion PR can carry several tickets
    const qa = await run(['qa'], { cwd: dir, env: { ...base, PR_URL: 'https://github.com/o/docs/pull/9', QA_URL: 'https://example.org/documentation/qa/', PR_BODY: `x\n${o.marker}\n<!-- multisync:ticket=desk:999 -->` } });
    assert.strictEqual(qa.code, 0);
    assert.match(t.comments.at(-1), /Deployed to QA/);
    assert.match(t.comments.at(-1), /documentation\/qa/);
    assert.strictEqual(t.done, false);

    const approved = await run(['approve'], { cwd: dir, env: { ...base, PR_URL: 'https://github.com/o/docs/pull/9', PR_BODY: `intro\n${o.marker}` } });
    assert.strictEqual(JSON.parse(approved.out).ok, true);
    assert.strictEqual(t.done, true);
    assert.match(t.comments.at(-1), /Approved/);

    const none = await run(['approve'], { cwd: dir, env: { ...base, PR_URL: 'u', PR_BODY: 'no marker here' } });
    assert.strictEqual(none.code, 0);
    assert.strictEqual(JSON.parse(none.out).skipped, true);

    const dead = await run(['approve'], { cwd: dir, env: { ...base, CAIMANDESK_URL: 'http://127.0.0.1:9', PR_URL: 'u', PR_BODY: o.marker } });
    assert.strictEqual(dead.code, 0, 'a dead ticket system must not fail the workflow');
    assert.strictEqual(JSON.parse(dead.out).ok, false);

    const unconfigured = await run(['open'], { cwd: dir, env: { ...process.env, CAIMANDESK_API_TOKEN: '', CAIMANDESK_PROJECT_ID: '', CAIMANDESK_MCP_URL: '', PR_URL: 'u' } });
    assert.strictEqual(unconfigured.code, 0);
  } finally { v.server.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});
