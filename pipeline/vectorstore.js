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
  constructor(cfg, dim, fetchImpl = globalThis.fetch, collection = cfg.collection) {
    this.cfg = cfg;
    this.dim = dim;
    this.fetch = fetchImpl;
    this.collection = collection;
  }

  /**
   * One request, retried on transient failures (408, 425, 429, 5xx, network errors) with exponential backoff.
   * A long run must not die because of one dropped connection or a busy server.
   */
  async _req(method, path, body, { retries = 4, sleep = (ms) => new Promise((r) => setTimeout(r, ms)) } = {}) {
    for (let attempt = 0; ; attempt++) {
      let res; let text;
      try {
        res = await this.fetch(`${this.cfg.url}${path}`, {
          method,
          headers: { 'Content-Type': 'application/json', ...(this.cfg.apiKey ? { 'api-key': this.cfg.apiKey } : {}) },
          body: body ? JSON.stringify(body) : undefined,
        });
        text = await res.text();
      } catch (err) {
        if (attempt >= retries) throw err;
        await sleep(Math.min(500 * 2 ** attempt, 8000));
        continue;
      }
      if (res.ok) return text ? JSON.parse(text) : {};
      if ([408, 425, 429, 500, 502, 503, 504].includes(res.status) && attempt < retries) {
        await sleep(Math.min(500 * 2 ** attempt, 8000));
        continue;
      }
      throw new Error(`Qdrant ${method} ${path} -> ${res.status}: ${text.slice(0, 300)}`);
    }
  }

  async health() {
    const res = await this.fetch(`${this.cfg.url}/readyz`);
    return res.ok;
  }

  async ensureCollection() {
    const c = this.collection;
    const res = await this.fetch(`${this.cfg.url}/collections/${c}`, {
      headers: this.cfg.apiKey ? { 'api-key': this.cfg.apiKey } : {},
    });
    if (res.ok) return;
    await this._req('PUT', `/collections/${c}`, { vectors: { size: this.dim, distance: 'Cosine' } });
    for (const field of ['repo', 'path', 'commit', 'kind']) {
      await this._req('PUT', `/collections/${c}/index`, { field_name: field, field_schema: 'keyword' });
    }
  }

  /** Batched: Qdrant rejects a request over 32 MB, which a large repository exceeds in one go. */
  async upsert(points, batch = 256) {
    for (let i = 0; i < points.length; i += batch) {
      await this._req('PUT', `/collections/${this.collection}/points?wait=true`, { points: points.slice(i, i + batch) });
    }
  }

  /** @returns {Promise<{id:string, score:number, payload:object}[]>} */
  /** @param {{limit?:number, repo?:string, path?:string, kind?:string|string[]}} o */
  async search(vector, { limit = 5, repo, path: docPath, kind } = {}) {
    const must = [];
    if (repo) must.push({ key: 'repo', match: { value: repo } });
    if (docPath) must.push({ key: 'path', match: { value: docPath } });
    if (kind) must.push({ key: 'kind', match: Array.isArray(kind) ? { any: kind } : { value: kind } });
    const filter = must.length ? { must } : undefined;
    const out = await this._req('POST', `/collections/${this.collection}/points/search`, {
      vector, limit, with_payload: true, filter,
    });
    return out.result.map((r) => ({ id: r.id, score: r.score, payload: r.payload }));
  }

  /** Anti-staleness: re-key unchanged chunks to the new commit without re-embedding. */
  async touchCommit(ids, commit) {
    if (!ids.length) return;
    await this._req('POST', `/collections/${this.collection}/points/payload?wait=true`, {
      payload: { commit, refreshed_at: new Date().toISOString() },
      points: ids,
    });
  }

  async deleteByPath(repo, filePath) {
    await this._req('POST', `/collections/${this.collection}/points/delete?wait=true`, {
      filter: { must: [{ key: 'repo', match: { value: repo } }, { key: 'path', match: { value: filePath } }] },
    });
  }

  async deleteByRepo(repo, kind) {
    const must = [{ key: 'repo', match: { value: repo } }];
    if (kind) must.push({ key: 'kind', match: { value: kind } });
    await this._req('POST', `/collections/${this.collection}/points/delete?wait=true`, { filter: { must } });
  }

  async count(repo, kind) {
    const must = [];
    if (repo) must.push({ key: 'repo', match: { value: repo } });
    if (kind) must.push({ key: 'kind', match: { value: kind } });
    const out = await this._req('POST', `/collections/${this.collection}/points/count`, {
      exact: true,
      filter: must.length ? { must } : undefined,
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
  async search(vector, { limit = 5, repo, path: docPath, kind } = {}) {
    const kinds = kind ? [].concat(kind) : null;
    return [...this.points.values()]
      .filter((p) => (!repo || p.payload.repo === repo) && (!docPath || p.payload.path === docPath) && (!kinds || kinds.includes(p.payload.kind)))
      .map((p) => ({ id: p.id, score: cosine(vector, p.vector), payload: p.payload }))
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
  }
  async touchCommit(ids, commit) { for (const id of ids) { const p = this.points.get(id); if (p) p.payload.commit = commit; } }
  async deleteByPath(repo, filePath) {
    for (const [id, p] of this.points) if (p.payload.repo === repo && p.payload.path === filePath) this.points.delete(id);
  }
  async deleteByRepo(repo, kind) {
    for (const [id, p] of this.points) if (p.payload.repo === repo && (!kind || p.payload.kind === kind)) this.points.delete(id);
  }
  async count(repo, kind) { return [...this.points.values()].filter((p) => (!repo || p.payload.repo === repo) && (!kind || p.payload.kind === kind)).length; }
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
    payload: { repo, path: filePath, chunk: i, heading: c.heading, text: c.text, commit, kind: 'approved', approved_at: new Date().toISOString() },
  })));
  return chunks.length;
}

module.exports = { QdrantStore, MemoryVectorStore, indexApproved };
