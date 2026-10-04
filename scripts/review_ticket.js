#!/usr/bin/env node
'use strict';
/**
 * Review-ticket lifecycle for a docs PR waiting in QA. Never fails a workflow because a ticket could not be made.
 *
 *   open     after the PR is created: create (or update) the ticket, print JSON { ref, url, created, marker }
 *            env: PR_URL, RESULTS_FILE (default pipeline-results.json), ENVIRONMENT (default QA)
 *   qa       the docs PR was merged into the QA branch:   env: PR_URL, PR_BODY, QA_URL (ticket noted, stays open)
 *   approve  the promotion PR was merged to production:   env: PR_URL, PR_BODY (every ticket in the body is closed)
 *   reject   a PR was closed without merging:             env: PR_URL, PR_BODY (noted, stays open)
 *
 * The ticket is found again through a marker the workflow stores in the PR body: <!-- multisync:ticket=desk:42 -->
 */
const fs = require('fs');
const { loadConfig } = require('../pipeline/config');
const { createTickets, ticketRefsFromBody, ticketMarker } = require('../pipeline/tickets');

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

  const refs = ticketRefsFromBody(env.PR_BODY);
  if (!refs.length) { console.error('no ticket marker in the PR body; nothing to update'); return { skipped: true }; }
  const verbs = { qa: (r) => tickets.qaDeployed(r, { prUrl, siteUrl: env.QA_URL }), approve: (r) => tickets.approve(r, { prUrl }), reject: (r) => tickets.reject(r, { prUrl }) };
  if (!verbs[action]) throw new Error(`unknown action: ${action}`);
  const done = [];
  for (const r of refs) done.push({ id: r.id, ok: await verbs[action](r).catch((e) => { console.error(`ticket ${r.id}: ${e.message}`); return false; }) });
  return { ok: done.every((x) => x.ok), tickets: done };
}

if (require.main === module) {
  main(process.argv[2]).then((r) => { console.log(JSON.stringify(r)); })
    .catch((e) => { console.error(`ticket step failed (non-fatal): ${e.message}`); console.log(JSON.stringify({ skipped: true, error: e.message })); });
}

module.exports = { main };
