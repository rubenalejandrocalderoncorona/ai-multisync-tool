#!/usr/bin/env node
'use strict';
/** GitHub Actions step summary built from pipeline-results.json. */
const fs = require('fs');

let data;
try {
  data = JSON.parse(fs.readFileSync('pipeline-results.json', 'utf-8'));
} catch (e) {
  console.error(`could not read pipeline-results.json: ${e.message}`);
  process.exit(1);
}

const { results, repo, commit, runId, trust } = data;
const icon = { published: '✅', pending_review: '👀', refreshed: '♻️', skipped: '⏭️', fallback: '🚫' };
const out = (s = '') => process.stdout.write(`${s}\n`);
const pct = (n) => (typeof n === 'number' ? n.toFixed(2) : '—');

out('# Documentation Sync Summary');
out();
out('| Field | Value |\n| :--- | :--- |');
out(`| Run | \`${runId}\` |`);
out(`| Source | \`${repo}\` @ \`${String(commit).slice(0, 7)}\` |`);
out(`| Trust level | \`${trust}\` |`);
out();
out('| Outcome | Count |\n| :--- | ---: |');
for (const o of Object.keys(icon)) out(`| ${icon[o]} ${o} | ${results.filter((r) => r.outcome === o).length} |`);
out();

out('## Decisions');
out();
out('| File | Outcome | Reviewer action | Tag | Precision | Recall | Why |\n| :--- | :--- | :--- | :--- | ---: | ---: | :--- |');
for (const r of results) {
  const f = r.metrics?.final || {};
  out(`| \`${r.path}\` | ${icon[r.outcome] || ''} ${r.outcome} | ${r.reviewerAction} | ${r.rootCauseTag ? `\`${r.rootCauseTag}\`` : '—'} | ${pct(f.precision)} | ${pct(f.recall)} | ${String(r.reason).replace(/\|/g, '\\|')} |`);
}

const fallbacks = results.filter((r) => r.outcome === 'fallback');
if (fallbacks.length) {
  out();
  out('## Needs a human');
  out();
  for (const r of fallbacks) out(`- \`${r.path}\` — \`${r.rootCauseTag}\`${r.ticket ? ` → ${r.ticket}` : ''}`);
}
out();
out(`*Completed ${new Date().toISOString()}*`);
