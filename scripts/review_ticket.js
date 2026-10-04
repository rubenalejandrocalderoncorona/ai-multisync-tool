#!/usr/bin/env node
'use strict';
/**
 * Review-ticket lifecycle for a docs PR waiting in QA. Never fails a workflow because a ticket could not be made.
 *
 *   open     after the PR is created: create (or update) the ticket, print JSON { ref, url, created, marker }
 *            env: PR_URL, RESULTS_FILE (default pipeline-results.json), ENVIRONMENT (default QA)
 *   approve  the PR was merged:                 env: PR_URL, PR_BODY
 *   reject   the PR was closed without merging: env: PR_URL, PR_BODY
 *
 * The ticket is found again through a marker the workflow stores in the PR body: <!-- multisync:ticket=desk:42 -->
 */
const fs = require('fs');
const { loadConfig } = require('../pipeline/config');
const { createTickets, ticketRefFromBody, ticketMarker } = require('../pipeline/tickets');

async function main(action, env = process.env) {
  const cfg = loadConfig(env);
  const tickets = createTickets(cfg.alerts);
  const prUrl = env.PR_URL || '';
  if (!tickets.enabled) { console.error('review ticket skipped: ticketing is not configured'); return { skipped: true }; }

  if (action === 'open') {
    const results = JSON.parse(fs.readFileSync(env.RESULTS_FILE || 'pipeline-results.json', 'utf-8'));
    const items = results.results.filter((r) => r.outcome === 'pending_review')
      .map((r) => ({ path: r.path, ...(r.metrics?.final || {}) }));
    const t = await tickets.openReview({ repo: results.repo, commit: results.commit, prUrl, items, environment: env.ENVIRONMENT || 'QA' });
    return { ...t, marker: ticketMarker(t) };
  }

  const ref = ticketRefFromBody(env.PR_BODY);
  if (!ref) { console.error('no ticket marker in the PR body; nothing to update'); return { skipped: true }; }
  if (action === 'approve') return { ok: await tickets.approve(ref, { prUrl }) };
  if (action === 'reject') return { ok: await tickets.reject(ref, { prUrl }) };
  throw new Error(`unknown action: ${action}`);
}

if (require.main === module) {
  main(process.argv[2]).then((r) => { console.log(JSON.stringify(r)); })
    .catch((e) => { console.error(`ticket step failed (non-fatal): ${e.message}`); console.log(JSON.stringify({ skipped: true, error: e.message })); });
}

module.exports = { main };
