'use strict';
/**
 * Live test against the REAL cAImanDesk (Vikunja) MCP. Gated: it writes one clearly labelled test task,
 * exercises the full ticket lifecycle, then DELETES it. Run it yourself:
 *
 *   CAIMANDESK_LIVE=1 CAIMANDESK_API_TOKEN=tk_... CAIMANDESK_PROJECT_ID=1 npm run test:integration
 */
const test = require('node:test');
const assert = require('node:assert');
const crypto = require('node:crypto');
const { loadConfig } = require('../../pipeline/config');
const { createTickets } = require('../../pipeline/tickets');
const { withMcp } = require('../../pipeline/mcpclient');

const LIVE = process.env.CAIMANDESK_LIVE === '1' && process.env.CAIMANDESK_API_TOKEN && process.env.CAIMANDESK_PROJECT_ID;
const opts = { skip: LIVE ? false : 'set CAIMANDESK_LIVE=1, CAIMANDESK_API_TOKEN and CAIMANDESK_PROJECT_ID' };

test('REAL cAImanDesk MCP: review ticket lifecycle (create, find, comment, close) and cleanup', opts, async () => {
  const alerts = loadConfig(process.env).alerts;
  const t = createTickets(alerts);
  assert.strictEqual(t.transport, 'mcp');
  const repo = `itest/${crypto.randomBytes(3).toString('hex')}`;
  const review = { repo, commit: 'abcdef1234', prUrl: 'https://github.com/example/docs/pull/0', environment: 'TEST', items: [{ path: 'overview.md', precision: 1, recall: 0.9, style: 1, quality: 1 }] };
  let id;
  try {
    const first = await t.openReview(review);
    id = first.id;
    assert.strictEqual(first.created, true);
    assert.match(first.url, new RegExp(`/tasks/${id}$`));

    const again = await t.openReview(review);
    assert.strictEqual(again.id, id, 'the open ticket is found again, no duplicate');
    assert.strictEqual(again.created, false);

    await withMcp(alerts.deskMcpUrl, async (call) => {
      const task = await call('tasks_read_one', { id });
      assert.strictEqual(task.title, `[docs-review] ${repo} @ abcdef1`);
      assert.match(task.description, /docs-review|waiting for review|pull\/0/);
      const comments = await call('tasks_comments_read_all', { task_id: id });
      assert.ok((Array.isArray(comments) ? comments : [comments]).some((c) => /Pull request updated/.test(c.comment)), 'the repeat became a real comment');
    }, { headers: { Authorization: `Bearer ${alerts.deskToken}` } });

    assert.strictEqual(await t.approve({ id }, { prUrl: review.prUrl }), true);
    const after = await withMcp(alerts.deskMcpUrl, (call) => call('tasks_read_one', { id }), { headers: { Authorization: `Bearer ${alerts.deskToken}` } });
    assert.strictEqual(after.done, true);
  } finally {
    if (id) await withMcp(alerts.deskMcpUrl, (call) => call('tasks_delete', { id }), { headers: { Authorization: `Bearer ${alerts.deskToken}` } }).catch((e) => console.error(`cleanup failed for task ${id}: ${e.message}`));
  }
});
