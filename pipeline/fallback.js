'use strict';
/**
 * Fallback / remediation. A fallback decision NEVER publishes: it opens a ticket for the
 * technical-writer queue (draft + failed checks), pings Slack, and leaves everything
 * unmerged so a human must act before anything ships. Delivery failures are logged,
 * never thrown — the run's decision log in the FactStore remains the source of truth.
 */

function ticketBody(d) {
  const attempts = (d.attempts || []).map((a) =>
    `| ${a.n}${a.widened ? ' (widened)' : ''} | ${a.precision?.toFixed(2)} | ${a.recall?.toFixed(2)} | ${a.style?.toFixed(2)} | ${a.quality?.toFixed(2)} | ${a.failure || 'pass'} |`).join('\n');
  return [
    `**Repo:** ${d.repo}`, `**File:** ${d.path}`, `**Commit:** ${d.commit}`,
    `**Reviewer action:** ${d.reviewerAction}`, `**Root cause tag:** \`${d.rootCauseTag}\``,
    `**Reason:** ${d.reason}`, '',
    attempts && '| attempt | precision | recall | style | quality | failed check |\n|---|---|---|---|---|---|\n' + attempts,
    d.feedback?.length && `\n**Judge findings**\n${d.feedback.map((f) => `- ${f}`).join('\n')}`,
    d.draft && `\n<details><summary>Last draft</summary>\n\n\`\`\`markdown\n${d.draft.slice(0, 20000)}\n\`\`\`\n</details>`,
  ].filter(Boolean).join('\n');
}

async function openGithubIssue(d, a, fetchImpl) {
  if (!a.githubToken || !a.githubRepo) return null;
  const res = await fetchImpl(`https://api.github.com/repos/${a.githubRepo}/issues`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${a.githubToken}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: `[docs-sync] ${d.rootCauseTag}: ${d.repo} ${d.path}`, body: ticketBody(d), labels: ['docs-sync', 'needs-technical-writer'] }),
  });
  return res.ok ? (await res.json()).html_url : null;
}

async function openJiraIssue(d, a, fetchImpl) {
  if (!a.jiraBaseUrl || !a.jiraToken || !a.jiraProject) return null;
  const res = await fetchImpl(`${a.jiraBaseUrl}/rest/api/2/issue`, {
    method: 'POST',
    headers: { Authorization: `Basic ${Buffer.from(`${a.jiraEmail}:${a.jiraToken}`).toString('base64')}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields: { project: { key: a.jiraProject }, issuetype: { name: 'Task' }, summary: `[docs-sync] ${d.rootCauseTag}: ${d.repo} ${d.path}`, description: ticketBody(d), labels: ['docs-sync'] } }),
  });
  return res.ok ? `${a.jiraBaseUrl}/browse/${(await res.json()).key}` : null;
}

const { createTickets, fallbackHtml } = require('./tickets');
const ticketHtml = fallbackHtml;

async function escalate(d, alerts, fetchImpl = globalThis.fetch) {
  let ticket = null;
  try {
    if (alerts.ticketProvider === 'caimandesk') ticket = await createTickets(alerts, fetchImpl).openFallback(d);
    if (alerts.ticketProvider === 'github') ticket = await openGithubIssue(d, alerts, fetchImpl);
    if (alerts.ticketProvider === 'jira') ticket = await openJiraIssue(d, alerts, fetchImpl);
  } catch (e) {
    console.warn(`ticket creation failed: ${e.message}`);
  }
  if (alerts.slackWebhook) {
    try {
      await fetchImpl(alerts.slackWebhook, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: `:warning: docs-sync *${d.rootCauseTag}* — ${d.repo}/${d.path}\n${d.reason}${ticket ? `\n${ticket}` : ''}` }),
      });
    } catch (e) {
      console.warn(`slack alert failed: ${e.message}`);
    }
  }
  return ticket;
}

module.exports = { escalate, ticketBody, ticketHtml };
