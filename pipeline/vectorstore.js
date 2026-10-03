'use strict';
const { chunkMarkdown, pointId } = require('./chunker');
const { cosine } = require('./llm');

/**
 * Qdrant over its REST API (no SDK dependency).
 * Rules enforced here:
 *   - Only *approved* documentation text is ever indexed (callers' responsibility,
 *     see indexApproved) — never drafts, never GAR hypothetical paragraphs.
 *   - Every point carries the source `commit`; a mismatch means the chunk is stale.
 */
class QdrantStore {
  constructor(cfg, dim, fetchImpl = globalThis.fetch) {
    this.cfg = cfg;
    this.dim = dim;
    this.fetch = fetchImpl;
  }

  async _req(method, path, body) {
    const res = await this.fetch(`${this.cfg.url}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', ...(this.cfg.apiKey ? { 'api-key': this.cfg.apiKey } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(`Qdrant ${method} ${path} -> ${res.status}: ${text.slice(0, 300)}`);
    return text ? JSON.parse(text) : {};
  }

  async health() {
    const res = await this.fetch(`${this.cfg.url}/readyz`);
    return res.ok;
  }

  async ensureCollection() {
    const c = this.cfg.collection;
    const res = await this.fetch(`${this.cfg.url}/collections/${c}`, {
      headers: this.cfg.apiKey ? { 'api-key': this.cfg.apiKey } : {},
    });
    if (res.ok) return;
    await this._req('PUT', `/collections/${c}`, { vectors: { size: this.dim, distance: 'Cosine' } });
    for (const field of ['repo', 'path', 'commit']) {
      await this._req('PUT', `/collections/${c}/index`, { field_name: field, field_schema: 'keyword' });
    }
  }

  async upsert(points) {
    if (!points.length) return;
    await this._req('PUT', `/collections/${this.cfg.collection}/points?wait=true`, { points });
  }

  /** @returns {Promise<{id:string, score:number, payload:object}[]>} */
  async search(vector, { limit = 5, repo, path: docPath } = {}) {
    const must = [];
    if (repo) must.push({ key: 'repo', match: { value: repo } });
    if (docPath) must.push({ key: 'path', match: { value: docPath } });
    const filter = must.length ? { must } : undefined;
    const out = await this._req('POST', `/collections/${this.cfg.collection}/points/search`, {
      vector, limit, with_payload: true, filter,
    });
    return out.result.map((r) => ({ id: r.id, score: r.score, payload: r.payload }));
  }

  /** Anti-staleness: re-key unchanged chunks to the new commit without re-embedding. */
  async touchCommit(ids, commit) {
    if (!ids.length) return;
    await this._req('POST', `/collections/${this.cfg.collection}/points/payload?wait=true`, {
      payload: { commit, refreshed_at: new Date().toISOString() },
      points: ids,
    });
  }

  async deleteByPath(repo, filePath) {
    await this._req('POST', `/collections/${this.cfg.collection}/points/delete?wait=true`, {
      filter: { must: [{ key: 'repo', match: { value: repo } }, { key: 'path', match: { value: filePath } }] },
    });
  }

  async count(repo) {
    const out = await this._req('POST', `/collections/${this.cfg.collection}/points/count`, {
      exact: true,
      filter: repo ? { must: [{ key: 'repo', match: { value: repo } }] } : undefined,
    });
    return out.result.count;
  }
}

/** In-memory twin used by unit tests and the infra-free demo mode. */
class MemoryVectorStore {
  constructor() { this.points = new Map(); }
  async health() { return true; }
  async ensureCollection() {}
  async upsert(points) { for (const p of points) this.points.set(p.id, p); }
  async search(vector, { limit = 5, repo, path: docPath } = {}) {
    return [...this.points.values()]
      .filter((p) => (!repo || p.payload.repo === repo) && (!docPath || p.payload.path === docPath))
      .map((p) => ({ id: p.id, score: cosine(vector, p.vector), payload: p.payload }))
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
  }
  async touchCommit(ids, commit) { for (const id of ids) { const p = this.points.get(id); if (p) p.payload.commit = commit; } }
  async deleteByPath(repo, filePath) {
    for (const [id, p] of this.points) if (p.payload.repo === repo && p.payload.path === filePath) this.points.delete(id);
  }
  async count(repo) { return [...this.points.values()].filter((p) => !repo || p.payload.repo === repo).length; }
}

/**
 * Index an approved document. Replaces the file's previous chunks so the index
 * always mirrors the final published text, keyed by commit hash.
 */
async function indexApproved({ store, llm, repo, filePath, content, commit }) {
  const chunks = chunkMarkdown(content);
  await store.deleteByPath(repo, filePath);
  if (!chunks.length) return 0;
  const vectors = await llm.embed(chunks.map((c) => `${c.heading}\n${c.text}`));
  await store.upsert(chunks.map((c, i) => ({
    id: pointId(repo, filePath, i),
    vector: vectors[i],
    payload: { repo, path: filePath, chunk: i, heading: c.heading, text: c.text, commit, approved_at: new Date().toISOString() },
  })));
  return chunks.length;
}

module.exports = { QdrantStore, MemoryVectorStore, indexApproved };
