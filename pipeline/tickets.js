'use strict';
/**
 * Ticketing port for cAImanDesk (a Vikunja deployment). Three events, two interchangeable transports.
 *
 *   events      openFallback(decision)        a run failed; a human must act
 *               openReview({...})             a docs PR is waiting in QA for review
 *               approve(ref) / reject(ref)    the PR was merged / closed unmerged
 *   transports  mcp   Vikunja's built-in MCP server, /api/v2/mcp (tools tasks_create, tasks_read_all, tasks_update, tasks_comments_create)
 *               rest  the Vikunja REST API with an API token
 *
 * Both transports behave identically: duplicates are never opened for the same title, and
 * ticket creation never fails or blocks a run (callers get null and a warning).
 */
const { withMcp } = require('./mcpclient');

const esc = (t) => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const MARKER = /<!--\s*multisync:ticket=([a-z]+):(\d+)\s*-->/;

// ── transports ────────────────────────────────────────────────────────────────
/** REST: PUT /projects/{id}/tasks, POST /tasks/{id}, comments on repeats. */
function restBackend(a, fetchImpl) {
  const headers = { Authorization: `Bearer ${a.deskToken}`, 'Content-Type': 'application/json' };
  const api = `${a.deskBaseUrl}/api/v1`;
  const json = async (res) => (res.ok ? res.json() : null);
  return {
    name: 'rest',
    async run(fn) {
      return fn({
        async findOpen(projectId, title) {
          const res = await fetchImpl(`${api}/projects/${projectId}/tasks?s=${encodeURIComponent(title)}&per_page=20`, { headers });
          const tasks = (await json(res)) || [];
          return tasks.find((t) => t.title === title && !t.done) || null;
        },
        async create(projectId, { title, description, priority }) {
          const res = await fetchImpl(`${api}/projects/${projectId}/tasks`, { method: 'PUT', headers, body: JSON.stringify({ title, description, priority }) });
          if (!res.ok) throw new Error(`cAImanDesk ${res.status}: ${(await res.text()).slice(0, 200)}`);
          return res.json();
        },
        async note(task, html) {
          await fetchImpl(`${api}/tasks/${task.id}/comments`, { method: 'PUT', headers, body: JSON.stringify({ comment: html }) });
        },
        async setDone(task, done) {
          // v1 task update replaces every column, so read-modify-write
          const cur = await (await fetchImpl(`${api}/tasks/${task.id}`, { headers })).json();
          await fetchImpl(`${api}/tasks/${task.id}`, { method: 'POST', headers, body: JSON.stringify({ ...cur, done }) });
        },
      });
    },
  };
}

/** MCP: Vikunja's built-in server (/api/v2/mcp). Real comments and true partial updates, so it behaves exactly like REST. */
function mcpBackend(a) {
  const headers = a.deskToken ? { Authorization: `Bearer ${a.deskToken}` } : {};
  return {
    name: 'mcp',
    async run(fn) {
      return withMcp(a.deskMcpUrl, (call) => fn({
        async findOpen(projectId, title) {
          const tasks = await call('tasks_read_all', { project_id: Number(projectId), search: title, filter: 'done = false', per_page: 50 });
          return (Array.isArray(tasks) ? tasks : []).find((t) => t.title === title && !t.done) || null;
        },
        async create(projectId, { title, description, priority }) {
          return call('tasks_create', { project_id: Number(projectId), title, description, priority });
        },
        async note(task, html) {
          await call('tasks_comments_create', { task_id: Number(task.id), comment: html });
        },
        async setDone(task, done) {
          await call('tasks_update', { id: Number(task.id), done });
        },
      }), { headers });
    },
  };
}

function pickBackend(a, fetchImpl) {
  if (!a.deskToken || !a.deskProjectId) return null;
  return (a.deskTransport || 'mcp') === 'rest' ? restBackend(a, fetchImpl) : mcpBackend(a);
}

// ── bodies ────────────────────────────────────────────────────────────────────
function fallbackHtml(d) {
  const rows = (d.attempts || []).map((x) => `<tr><td>${x.n}${x.widened ? ' (widened)' : ''}</td><td>${x.precision?.toFixed(2)}</td><td>${x.recall?.toFixed(2)}</td><td>${x.style?.toFixed(2)}</td><td>${x.quality?.toFixed(2)}</td><td>${esc(x.failure || 'pass')}</td></tr>`).join('');
  return [
    `<p><strong>Repo:</strong> ${esc(d.repo)}<br><strong>File:</strong> ${esc(d.path)}<br><strong>Commit:</strong> ${esc(d.commit)}<br><strong>Reviewer action:</strong> ${esc(d.reviewerAction)}<br><strong>Root cause:</strong> <code>${esc(d.rootCauseTag)}</code><br><strong>Reason:</strong> ${esc(d.reason)}</p>`,
    rows && `<table><tr><th>attempt</th><th>precision</th><th>recall</th><th>style</th><th>quality</th><th>failed check</th></tr>${rows}</table>`,
    d.feedback?.length && `<p><strong>Judge findings</strong></p><ul>${d.feedback.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>`,
    d.draft && `<details><summary>Last draft</summary><pre>${esc(d.draft.slice(0, 15000))}</pre></details>`,
  ].filter(Boolean).join('');
}

function reviewHtml({ repo, commit, prUrl, items = [], environment = 'QA' }) {
  const rows = items.map((i) => `<tr><td><code>${esc(i.path)}</code></td><td>${i.precision?.toFixed(2) ?? '-'}</td><td>${i.recall?.toFixed(2) ?? '-'}</td><td>${i.style?.toFixed(2) ?? '-'}</td><td>${i.quality?.toFixed(2) ?? '-'}</td></tr>`).join('');
  return [
    `<p>Generated documentation from <strong>${esc(repo)}</strong> at <code>${esc(String(commit).slice(0, 7))}</code> is waiting for review in <strong>${esc(environment)}</strong>.</p>`,
    `<p><strong>Pull request:</strong> <a href="${esc(prUrl)}">${esc(prUrl)}</a></p>`,
    rows && `<table><tr><th>page</th><th>precision</th><th>recall</th><th>style</th><th>quality</th></tr>${rows}</table>`,
    '<p>Approving means merging the pull request. This ticket is closed automatically when that happens; if the pull request is closed without merging, a note is added here and the ticket stays open.</p>',
  ].filter(Boolean).join('');
}

// ── service ───────────────────────────────────────────────────────────────────
function createTickets(alerts, fetchImpl = globalThis.fetch) {
  const backend = pickBackend(alerts, fetchImpl);
  const link = (t) => `${alerts.deskPublicUrl || alerts.deskBaseUrl}/tasks/${t.id}`;

  /** Open a task, or add a note to the open one with the same title. Returns { id, url, created }. */
  async function openOrNote(title, html, priority, noteHtml) {
    return backend.run(async (b) => {
      const existing = await b.findOpen(alerts.deskProjectId, title);
      if (existing) { await b.note(existing, noteHtml); return { id: existing.id, url: link(existing), created: false }; }
      const t = await b.create(alerts.deskProjectId, { title, description: html, priority });
      return { id: t.id, url: link(t), created: true };
    });
  }

  return {
    enabled: !!backend,
    transport: backend?.name || null,

    async openFallback(d) {
      if (!backend) return null;
      const title = `[docs-sync] ${d.rootCauseTag}: ${d.repo} ${d.path}`;
      const r = await openOrNote(title, fallbackHtml(d), d.rootCauseTag === 'pipeline_error' ? 4 : 3,
        `<p>Failed again at commit <code>${esc(d.commit)}</code>: ${esc(d.reason)}</p>`);
      return r.url;
    },

    /** @returns {Promise<{id:number,url:string,ref:string}|null>} ref is what the PR body carries to find the ticket later */
    async openReview({ repo, commit, prUrl, items, environment }) {
      if (!backend) return null;
      const title = `[docs-review] ${repo} @ ${String(commit).slice(0, 7)}`;
      const r = await openOrNote(title, reviewHtml({ repo, commit, prUrl, items, environment }), 2,
        `<p>Pull request updated: <a href="${esc(prUrl)}">${esc(prUrl)}</a></p>`);
      return { ...r, ref: `desk:${r.id}` };
    },

    /** The docs PR was merged into the QA branch: the page is live in QA, the ticket stays open until promotion. */
    async qaDeployed(ref, { prUrl, siteUrl } = {}) {
      if (!backend) return false;
      return backend.run(async (b) => {
        await b.note({ id: ref.id }, `<p><strong>Deployed to QA.</strong>${siteUrl ? ` Review it at <a href="${esc(siteUrl)}">${esc(siteUrl)}</a>.` : ''} Pull request merged: <a href="${esc(prUrl)}">${esc(prUrl)}</a>. It goes to production when the promotion pull request is merged.</p>`);
        return true;
      });
    },

    async approve(ref, { prUrl } = {}) {
      if (!backend) return false;
      return backend.run(async (b) => {
        const t = { id: ref.id };
        await b.note(t, `<p><strong>Approved for production.</strong> Pull request merged: <a href="${esc(prUrl)}">${esc(prUrl)}</a> (${new Date().toISOString().slice(0, 10)}).</p>`);
        await b.setDone(t, true);
        return true;
      });
    },

    async reject(ref, { prUrl } = {}) {
      if (!backend) return false;
      return backend.run(async (b) => {
        await b.note({ id: ref.id }, `<p><strong>Not approved.</strong> The pull request was closed without merging: <a href="${esc(prUrl)}">${esc(prUrl)}</a>. Regenerate or discard.</p>`);
        return true;
      });
    },
  };
}

/** Find the ticket reference a review PR carries in its body. */
function ticketRefFromBody(body) {
  const m = String(body || '').match(MARKER);
  return m ? { provider: m[1], id: Number(m[2]) } : null;
}

/** All distinct ticket references in a body: a promotion PR carries one marker per page batch it ships. */
function ticketRefsFromBody(body) {
  const seen = new Set();
  const out = [];
  for (const m of String(body || '').matchAll(new RegExp(MARKER.source, 'g'))) {
    const key = `${m[1]}:${m[2]}`;
    if (!seen.has(key)) { seen.add(key); out.push({ provider: m[1], id: Number(m[2]) }); }
  }
  return out;
}
const ticketMarker = (ref) => `<!-- multisync:ticket=${ref.ref || `desk:${ref.id}`} -->`;

module.exports = { createTickets, ticketRefFromBody, ticketRefsFromBody, ticketMarker, reviewHtml, fallbackHtml, pickBackend };
