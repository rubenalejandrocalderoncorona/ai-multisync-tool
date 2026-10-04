#!/usr/bin/env node
'use strict';
/**
 * Setup doctor: shows exactly which credentials and endpoints are missing or unreachable.
 * Never prints a secret value. Exit code 1 if a REQUIRED item is missing or failing.
 *
 *   node scripts/doctor.js            check env + reach every service
 *   node scripts/doctor.js --env-only check env only (no network)
 */
const { loadConfig } = require('../pipeline/config');

const ENV_ONLY = process.argv.includes('--env-only');
const cfg = loadConfig();
const env = process.env;
const has = (k) => !!env[k];
const rows = [];
const add = (area, item, required, status, hint) => rows.push({ area, item, required, status, hint });
const timeout = (ms) => AbortSignal.timeout(ms);

async function probe(fn) {
  try { return await fn(); } catch (e) { return { ok: false, why: e.cause?.code || e.name === 'TimeoutError' ? 'unreachable/timeout' : e.message }; }
}

async function main() {
  // ── LLM / embeddings ───────────────────────────────────────────────────────
  const aiKey = !!cfg.ai.apiKey;
  add('LLM', 'INTERNAL_AI_API_KEY (or AI_API_KEY)', true, aiKey ? 'set' : 'MISSING', 'Provider API key for the writer/judge/embeddings');
  if (aiKey && !ENV_ONLY) {
    const r = await probe(async () => {
      const res = await fetch(`${cfg.ai.baseUrl}/v1/models`, { headers: { Authorization: `Bearer ${cfg.ai.apiKey}` }, signal: timeout(8000) });
      return { ok: res.ok, why: `HTTP ${res.status}` };
    });
    add('LLM', `${cfg.ai.baseUrl} reachable + key accepted`, true, r.ok ? 'ok' : `FAIL (${r.why})`, 'Check AI_API_BASE_URL and the key');
    const e = await probe(async () => {
      const res = await fetch(`${cfg.ai.baseUrl}${cfg.ai.embedPath}`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${cfg.ai.apiKey}` }, body: JSON.stringify({ model: cfg.ai.embedModel, input: ['ping'] }), signal: timeout(10000) });
      if (!res.ok) return { ok: false, why: `HTTP ${res.status}` };
      const dim = (await res.json()).data?.[0]?.embedding?.length;
      return { ok: dim === cfg.ai.embedDim, why: `model returns ${dim} dims but AI_EMBED_DIM=${cfg.ai.embedDim}` };
    });
    add('LLM', `embeddings ${cfg.ai.embedModel} match AI_EMBED_DIM`, true, e.ok ? 'ok' : `FAIL (${e.why})`, 'AI_EMBED_DIM must equal the model dimension');
  }
  add('LLM', 'AI_MODEL / AI_FAST_MODEL', false, has('AI_MODEL') ? 'set' : `default (${cfg.ai.model})`, 'The judge should be a strong model; the fast model is used for GAR and routing');

  // ── Qdrant ─────────────────────────────────────────────────────────────────
  if (cfg.qdrant.driver === 'memory') add('Vector DB', 'VECTOR_DRIVER=memory', false, 'memory (nothing persists)', 'Rehearsal only');
  else {
    add('Vector DB', 'QDRANT_URL', true, has('QDRANT_URL') ? 'set' : 'MISSING (default http://localhost:6333)', 'URL of Qdrant on the VPS');
    add('Vector DB', 'QDRANT_API_KEY', false, has('QDRANT_API_KEY') ? 'set' : 'not set (fine if Qdrant is cluster-internal without auth)', 'Only if your Qdrant requires an API key');
    if (!ENV_ONLY) {
      const q = await probe(async () => {
        const res = await fetch(`${cfg.qdrant.url}/collections`, { headers: cfg.qdrant.apiKey ? { 'api-key': cfg.qdrant.apiKey } : {}, signal: timeout(6000) });
        return { ok: res.ok, why: `HTTP ${res.status}` };
      });
      add('Vector DB', `Qdrant reachable (${cfg.qdrant.collection} + ${cfg.qdrant.codeCollection})`, true, q.ok ? 'ok' : `FAIL (${q.why})`, 'In-cluster URL needs the in-cluster runner; from a laptop use an ssh tunnel');
    }
  }

  // ── FactStore ──────────────────────────────────────────────────────────────
  if (cfg.factstore.driver === 'memory') add('FactStore', 'FACTSTORE_DATABASE_URL', true, 'MISSING (memory driver: cross-repo gate and audit log will not persist)', 'postgres://user:pass@host:5432/factstore');
  else {
    add('FactStore', 'FACTSTORE_DATABASE_URL', true, 'set', '');
    if (!ENV_ONLY) {
      const p = await probe(async () => {
        const { Pool } = require('pg');
        const pool = new Pool({ connectionString: cfg.factstore.databaseUrl, connectionTimeoutMillis: 6000 });
        try { await pool.query('SELECT 1'); return { ok: true }; } catch (e) { return { ok: false, why: e.code || e.message }; } finally { await pool.end(); }
      });
      add('FactStore', 'Postgres reachable', true, p.ok ? 'ok' : `FAIL (${p.why})`, 'make stack-up on the VPS; Postgres must be reachable from the runner');
    }
  }

  // ── cAImanDesk ─────────────────────────────────────────────────────────────
  const a = cfg.alerts;
  if (a.ticketProvider === 'caimandesk') {
    add('Tickets', 'CAIMANDESK_API_TOKEN', true, a.deskToken ? 'set' : 'MISSING', 'cAImanDesk > Settings > API tokens (allow creating tasks)');
    add('Tickets', 'CAIMANDESK_PROJECT_ID', true, a.deskProjectId ? 'set' : 'MISSING', 'Numeric id in the project URL, e.g. /projects/7');
    if (!ENV_ONLY && a.deskToken && a.deskProjectId) {
      const t = await probe(async () => {
        const res = await fetch(`${a.deskBaseUrl}/api/v1/projects/${a.deskProjectId}`, { headers: { Authorization: `Bearer ${a.deskToken}` }, signal: timeout(8000) });
        return { ok: res.ok, why: `HTTP ${res.status}${res.status === 401 ? ' (token rejected)' : res.status === 404 ? ' (project not found or no access)' : ''}` };
      });
      add('Tickets', `${a.deskBaseUrl} project readable with token`, true, t.ok ? 'ok' : `FAIL (${t.why})`, 'Token needs access to that project');
    }
  } else add('Tickets', 'TICKET_PROVIDER', false, a.ticketProvider, 'Set TICKET_PROVIDER=caimandesk for the demo');
  add('Tickets', 'SLACK_WEBHOOK_URL', false, a.slackWebhook ? 'set' : 'not set (optional)', 'Optional alert channel');

  // ── GitHub ─────────────────────────────────────────────────────────────────
  add('GitHub', 'DOCS_SYNC_PAT (Actions secret)', !!env.CI, has('DOCS_SYNC_PAT') || has('GITHUB_TOKEN') ? 'set' : 'MISSING (set it as an Actions secret; not needed locally)', 'Fine-grained PAT: contents + pull requests + workflows on the central repo, read on source repos');

  // ── output ─────────────────────────────────────────────────────────────────
  const w = (k) => Math.max(...rows.map((r) => String(r[k]).length));
  const [wa, wi] = [w('area'), w('item')];
  let bad = 0;
  for (const r of rows) {
    const failing = r.required && /MISSING|FAIL/.test(r.status);
    if (failing) bad++;
    const mark = failing ? '✘' : /MISSING|FAIL/.test(r.status) ? '!' : '✔';
    console.log(`${mark} ${r.area.padEnd(wa)}  ${r.item.padEnd(wi)}  ${r.status}${failing && r.hint ? `\n  ${' '.repeat(wa)}  -> ${r.hint}` : ''}`);
  }
  console.log(`\n${bad ? `${bad} required item(s) need attention. See docs/SETUP-REQUIRED.md.` : 'All required items are in place.'}`);
  process.exit(bad ? 1 : 0);
}

main();
