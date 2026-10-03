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

const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Ticket body as HTML (cAImanDesk renders task descriptions as HTML, not Markdown). */
function ticketHtml(d) {
  const rows = (d.attempts || []).map((a) =>
    `<tr><td>${a.n}${a.widened ? ' (widened)' : ''}</td><td>${a.precision?.toFixed(2)}</td><td>${a.recall?.toFixed(2)}</td><td>${a.style?.toFixed(2)}</td><td>${a.quality?.toFixed(2)}</td><td>${esc(a.failure || 'pass')}</td></tr>`).join('');
  return [
    `<p><strong>Repo:</strong> ${esc(d.repo)}<br><strong>File:</strong> ${esc(d.path)}<br><strong>Commit:</strong> ${esc(d.commit)}<br>`
      + `<strong>Reviewer action:</strong> ${esc(d.reviewerAction)}<br><strong>Root cause:</strong> <code>${esc(d.rootCauseTag)}</code><br><strong>Reason:</strong> ${esc(d.reason)}</p>`,
    rows && `<table><tr><th>attempt</th><th>precision</th><th>recall</th><th>style</th><th>quality</th><th>failed check</th></tr>${rows}</table>`,
    d.feedback?.length && `<p><strong>Judge findings</strong></p><ul>${d.feedback.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>`,
    d.draft && `<details><summary>Last draft</summary><pre>${esc(d.draft.slice(0, 15000))}</pre></details>`,
  ].filter(Boolean).join('');
}

/**
 * cAImanDesk is a Vikunja v2 deployment: PUT /api/v1/projects/{id}/tasks with a Bearer API token.
 * A repeat failure for the same repo+file+tag comments on the open task instead of opening a duplicate.
 */
async function openDeskTask(d, a, fetchImpl) {
  if (!a.deskToken || !a.deskProjectId) return null;
  const headers = { Authorization: `Bearer ${a.deskToken}`, 'Content-Type': 'application/json' };
  const api = `${a.deskBaseUrl}/api/v1`;
  const title = `[docs-sync] ${d.rootCauseTag}: ${d.repo} ${d.path}`;

  const found = await fetchImpl(`${api}/projects/${a.deskProjectId}/tasks?s=${encodeURIComponent(title)}&per_page=20`, { headers });
  if (found.ok) {
    const open = (await found.json()).find((t) => t.title === title && !t.done);
    if (open) {
      await fetchImpl(`${api}/tasks/${open.id}/comments`, {
        method: 'PUT', headers,
        body: JSON.stringify({ comment: `<p>Failed again at commit <code>${esc(d.commit)}</code>: ${esc(d.reason)}</p>` }),
      });
      return `${a.deskBaseUrl}/tasks/${open.id}`;
    }
  }
  const res = await fetchImpl(`${api}/projects/${a.deskProjectId}/tasks`, {
    method: 'PUT', headers,
    body: JSON.stringify({ title, description: ticketHtml(d), priority: d.rootCauseTag === 'pipeline_error' ? 4 : 3 }),
  });
  if (!res.ok) throw new Error(`cAImanDesk ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return `${a.deskBaseUrl}/tasks/${(await res.json()).id}`;
}

async function escalate(d, alerts, fetchImpl = globalThis.fetch) {
  let ticket = null;
  try {
    if (alerts.ticketProvider === 'caimandesk') ticket = await openDeskTask(d, alerts, fetchImpl);
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
