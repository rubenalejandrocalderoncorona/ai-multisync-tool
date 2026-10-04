'use strict';
/**
 * FactStore: Postgres-backed store of atomic claims plus the decision audit log.
 * (This is our own component — not the academic FactScore method.)
 *
 * Tables are defined in infra/postgres/init.sql and auto-created by migrate().
 */
const fs = require('fs');
const path = require('path');

class PgFactStore {
  constructor(databaseUrl) {
    // Lazy require so unit tests / memory mode do not need the dependency installed.
    const { Pool } = require('pg');
    // Pin the schema on every connection so queries never touch other workloads' tables.
    this.pool = new Pool({ connectionString: databaseUrl, options: '-c search_path=multisync' });
  }

  async migrate() {
    const sql = fs.readFileSync(path.join(__dirname, '..', 'infra', 'postgres', 'init.sql'), 'utf-8');
    await this.pool.query(sql);
  }

  async health() { await this.pool.query('SELECT 1'); return true; }

  async saveClaims({ repo, filePath, commit, claims }) {
    for (const c of claims) {
      await this.pool.query(
        `INSERT INTO claims (repo, path, commit, claim, supported, evidence)
         VALUES ($1,$2,$3,$4,$5,$6)`,
        [repo, filePath, commit, c.text, !!c.supported, c.evidence || null],
      );
    }
  }

  /** Previously approved claims for a document (used as extra judge evidence). */
  async approvedClaims(repo, filePath, limit = 50) {
    const r = await this.pool.query(
      `SELECT claim FROM claims WHERE repo=$1 AND path=$2 AND supported AND approved
       ORDER BY created_at DESC LIMIT $3`, [repo, filePath, limit]);
    return r.rows.map((x) => x.claim);
  }

  async approveClaims(repo, filePath, commit) {
    await this.pool.query(
      `UPDATE claims SET approved=true WHERE repo=$1 AND path=$2 AND commit=$3 AND supported`,
      [repo, filePath, commit]);
  }

  /** True when an approved document of `repo` mentions `symbol`. Used by the cross-repo gate. */
  async repoDocumentsSymbol(repo, symbol) {
    const r = await this.pool.query(
      `SELECT 1 FROM claims WHERE repo=$1 AND approved AND claim ILIKE $2 LIMIT 1`,
      [repo, `%${symbol}%`]);
    return r.rowCount > 0;
  }

  async recordDecision(d) {
    await this.pool.query(
      `INSERT INTO decisions (run_id, repo, path, commit, outcome, reviewer_action, root_cause_tag, reason, metrics, attempts)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
      [d.runId, d.repo, d.path, d.commit, d.outcome, d.reviewerAction, d.rootCauseTag || null, d.reason,
        JSON.stringify(d.metrics || {}), JSON.stringify(d.attempts || [])]);
  }

  async getContextState(repo) {
    const r = await this.pool.query('SELECT repo, commit, files, chunks FROM context_state WHERE repo=$1', [repo]);
    return r.rows[0] || null;
  }

  async listContextState() {
    return (await this.pool.query('SELECT repo, commit, files, chunks, updated_at FROM context_state ORDER BY repo')).rows;
  }

  async setContextState(repo, { commit, files, chunks }) {
    await this.pool.query(
      `INSERT INTO context_state (repo, commit, files, chunks) VALUES ($1,$2,$3,$4)
       ON CONFLICT (repo) DO UPDATE SET commit=$2, files=$3, chunks=$4, updated_at=now()`,
      [repo, commit, files, chunks]);
  }

  async recordNodeLog(e) {
    await this.pool.query(
      `INSERT INTO node_logs (run_id, repo, path, commit, node, status, ms, note) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
      [e.runId, e.repo, e.path, e.commit, e.node, e.status, e.ms, JSON.stringify(e.note || {})]);
  }

  /** Repeated fallbacks per repo/tag: feeds the root-cause backlog. */
  async rootCauseBacklog(repo) {
    const r = await this.pool.query(
      `SELECT root_cause_tag, count(*)::int AS n FROM decisions
       WHERE repo=$1 AND outcome='fallback' GROUP BY 1 ORDER BY n DESC`, [repo]);
    return r.rows;
  }

  // ── code -> docs coupling ────────────────────────────────────────────────────
  /** Replace a repo's symbols: only `paths` when given (incremental), otherwise all of them (full re-index). */
  async replaceSymbols(repo, rows, { paths } = {}) {
    if (paths) await this.pool.query('DELETE FROM symbols WHERE repo=$1 AND path = ANY($2)', [repo, paths]);
    else await this.pool.query('DELETE FROM symbols WHERE repo=$1', [repo]);
    for (let i = 0; i < rows.length; i += 500) {
      const b = rows.slice(i, i + 500);
      await this.pool.query(
        `INSERT INTO symbols (repo, path, kind, name, sig_hash)
         SELECT $1, * FROM unnest($2::text[], $3::text[], $4::text[], $5::text[]) ON CONFLICT DO NOTHING`,
        [repo, b.map((r) => r.path), b.map((r) => r.kind), b.map((r) => r.name), b.map((r) => r.sig_hash || '')]);
    }
  }

  async knownSymbolNames() {
    return new Set((await this.pool.query('SELECT DISTINCT name FROM symbols')).rows.map((r) => r.name));
  }

  async replaceDocRefs(docRepo, docPath, symbols, kind) {
    await this.pool.query('DELETE FROM doc_refs WHERE doc_repo=$1 AND doc_path=$2', [docRepo, docPath]);
    if (!symbols.length) return;
    await this.pool.query(
      `INSERT INTO doc_refs (symbol, doc_repo, doc_path, kind) SELECT unnest($1::text[]), $2, $3, $4 ON CONFLICT DO NOTHING`,
      [symbols, docRepo, docPath, kind]);
  }

  /** Documents that mention any of `names`, optionally leaving out one repo's own documents. */
  async docRefs(names, { excludeRepo } = {}) {
    if (!names.length) return [];
    const r = await this.pool.query(
      `SELECT symbol, doc_repo, doc_path, kind FROM doc_refs WHERE symbol = ANY($1) AND ($2::text IS NULL OR doc_repo <> $2) ORDER BY symbol, doc_repo, doc_path`,
      [names, excludeRepo || null]);
    return r.rows;
  }

  async symbolInfo(name) {
    const defs = await this.pool.query('SELECT repo, path, kind FROM symbols WHERE name=$1 ORDER BY repo, path', [name]);
    const refs = await this.pool.query('SELECT doc_repo, doc_path, kind FROM doc_refs WHERE symbol=$1 ORDER BY doc_repo, doc_path', [name]);
    return { defined: defs.rows, referencedBy: refs.rows };
  }

  async couplingStats() {
    return (await this.pool.query(
      `SELECT s.repo, count(*)::int AS symbols, count(DISTINCT s.kind)::int AS kinds FROM symbols s GROUP BY s.repo ORDER BY s.repo`)).rows;
  }

  async close() { await this.pool.end(); }
}

class MemoryFactStore {
  constructor() { this.claims = []; this.decisions = []; this.nodeLogs = []; this.ctx = new Map(); }
  async getContextState(repo) { return this.ctx.get(repo) || null; }
  async setContextState(repo, st) { this.ctx.set(repo, { repo, ...st }); }
  async listContextState() { return [...this.ctx.values()]; }
  async recordNodeLog(e) { this.nodeLogs.push(e); }
  async migrate() {}
  async health() { return true; }
  async saveClaims({ repo, filePath, commit, claims }) {
    for (const c of claims) this.claims.push({ repo, path: filePath, commit, claim: c.text, supported: !!c.supported, approved: false });
  }
  async approvedClaims(repo, filePath) {
    return this.claims.filter((c) => c.repo === repo && c.path === filePath && c.supported && c.approved).map((c) => c.claim);
  }
  async approveClaims(repo, filePath, commit) {
    for (const c of this.claims) if (c.repo === repo && c.path === filePath && c.commit === commit && c.supported) c.approved = true;
  }
  async repoDocumentsSymbol(repo, symbol) {
    return this.claims.some((c) => c.repo === repo && c.approved && c.claim.toLowerCase().includes(symbol.toLowerCase()));
  }
  async recordDecision(d) { this.decisions.push(d); }
  async rootCauseBacklog(repo) {
    const m = {};
    for (const d of this.decisions) if (d.repo === repo && d.outcome === 'fallback') m[d.rootCauseTag] = (m[d.rootCauseTag] || 0) + 1;
    return Object.entries(m).map(([root_cause_tag, n]) => ({ root_cause_tag, n }));
  }
  // ── code -> docs coupling ────────────────────────────────────────────────────
  async replaceSymbols(repo, rows, { paths } = {}) {
    this.symbols = (this.symbols || []).filter((r) => !(r.repo === repo && (!paths || paths.includes(r.path))));
    for (const r of rows) if (!this.symbols.some((x) => x.repo === repo && x.path === r.path && x.kind === r.kind && x.name === r.name)) this.symbols.push({ repo, ...r });
  }
  async knownSymbolNames() { return new Set((this.symbols || []).map((r) => r.name)); }
  async replaceDocRefs(docRepo, docPath, symbols, kind) {
    this.refs = (this.refs || []).filter((r) => !(r.doc_repo === docRepo && r.doc_path === docPath));
    for (const symbol of symbols) this.refs.push({ symbol, doc_repo: docRepo, doc_path: docPath, kind });
  }
  async docRefs(names, { excludeRepo } = {}) {
    return (this.refs || []).filter((r) => names.includes(r.symbol) && (!excludeRepo || r.doc_repo !== excludeRepo));
  }
  async symbolInfo(name) {
    return { defined: (this.symbols || []).filter((r) => r.name === name).map(({ repo, path, kind }) => ({ repo, path, kind })), referencedBy: (this.refs || []).filter((r) => r.symbol === name).map(({ doc_repo, doc_path, kind }) => ({ doc_repo, doc_path, kind })) };
  }
  async couplingStats() {
    const m = {};
    for (const r of this.symbols || []) { m[r.repo] = m[r.repo] || { repo: r.repo, symbols: 0, kinds: new Set() }; m[r.repo].symbols++; m[r.repo].kinds.add(r.kind); }
    return Object.values(m).map((x) => ({ repo: x.repo, symbols: x.symbols, kinds: x.kinds.size }));
  }
  async close() {}
}

function createFactStore(cfg) {
  return cfg.driver === 'postgres' ? new PgFactStore(cfg.databaseUrl) : new MemoryFactStore();
}

module.exports = { PgFactStore, MemoryFactStore, createFactStore };
