'use strict';
/**
 * Context stage: make sure the vector database holds the WHOLE repository context before any
 * change is analysed, and retrieve from it afterwards.
 *
 * Two collections, two kinds of context:
 *   code context     (code collection)  every in-scope source file, chunked. "What the system does."
 *   semantic context (docs collection)  approved pages, the repo's own docs/README (kind source_doc),
 *                                       and page briefs. "What the docs should say and how."
 *
 * Indexing is incremental per commit. The first run, a forced run, or any run where the index is not
 * exactly at the previous commit (a missed run, a rebuilt DB) re-indexes the whole repo, so the index
 * heals itself instead of drifting.
 */
const { chunkCode, chunkMarkdown, pointId } = require('./chunker');
const { selectFiles, scrub, globToRegExp } = require('./codesource');

const matchesAny = (f, globs) => globs.some((g) => globToRegExp(g).test(f));

const BATCH = 64;
const MAX_FILE_BYTES = 200_000;
const DOC_FILE = /^(README(\.[a-z]+)?\.md|(docs|documentation)\/.*\.(md|mdx))$/i;
const LANG = { js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', py: 'python', go: 'go', java: 'java', rb: 'ruby', rs: 'rust', yaml: 'yaml', yml: 'yaml', json: 'json', sh: 'shell', sql: 'sql', md: 'markdown' };
const langOf = (f) => LANG[(f.split('.').pop() || '').toLowerCase()] || 'text';

async function embedAll(llm, texts) {
  const out = [];
  for (let i = 0; i < texts.length; i += BATCH) out.push(...(await llm.embed(texts.slice(i, i + BATCH))));
  return out;
}

/**
 * @param {object} a
 * @param {string} a.repo
 * @param {string} a.commit
 * @param {string} [a.before]      previous commit, '' when unknown
 * @param {boolean} [a.full]       force a whole-repo re-index
 * @param {object} a.git           { listFiles(rev), readAt(rev,file), changedBetween(a,b) }
 * @param {object} a.codeStore     code collection
 * @param {object} a.docStore      semantic collection
 * @param {object} a.llm           { embed }
 * @param {object} a.facts         FactStore (context_state)
 * @param {object} [a.pages]       policy pages (for briefs)
 * @param {string[]} [a.scope]     globs the index may read (union of the pages' scopes)
 * @param {string[]} [a.exclude]   globs that must never be read (repo-level exclude)
 * @param {string[]} [a.docs]      extra globs of existing documentation to index as semantic context (kind source_doc)
 * @param {{maxFiles?:number,maxChunks?:number}} [a.limits]
 */
async function syncContext({ repo, commit, before = '', full = false, git, codeStore, docStore, llm, facts, pages = [], scope = ['**'], exclude = [], docs = [], limits = {} }) {
  const started = Date.now();
  const maxFiles = limits.maxFiles ?? 3000;
  const maxChunks = limits.maxChunks ?? 8000;

  const all = git.listFiles(commit);
  // The index may only contain what the repo's declared pages are allowed to read.
  const allowed = new Set(selectFiles(all, { scope, exclude }));
  const codeFiles = [...allowed].filter((f) => !DOC_FILE.test(f));
  // Semantic sources are not limited to the pages' code scope: the README, docs/ and any `docs` globs the repo declares
  // (for example an existing end-user docs app). The repo-level exclude still applies.
  const docFiles = all.filter((f) => (DOC_FILE.test(f) || matchesAny(f, docs)) && !matchesAny(f, exclude));

  const state = await facts.getContextState(repo);
  const incremental = !full && !!before && !!state && state.commit === before;
  const changed = incremental ? new Set(git.changedBetween(before, commit)) : null;

  if (!incremental) {
    await codeStore.deleteByRepo(repo);
    await docStore.deleteByRepo(repo, 'source_doc');
    await docStore.deleteByRepo(repo, 'brief');
  }

  let removed = 0;
  if (incremental) {
    const present = new Set(all);
    const gone = [...changed].filter((f) => !present.has(f));
    // Only paths that could have been indexed count (tests, lockfiles and the like never were).
    for (const f of [...selectFiles(gone, { scope, exclude }), ...gone.filter((f) => DOC_FILE.test(f))]) {
      await codeStore.deleteByPath(repo, f); await docStore.deleteByPath(repo, f); removed++;
    }
  }

  const wanted = (f) => !incremental || changed.has(f);
  let toIndex = codeFiles.filter(wanted).slice(0, maxFiles);
  const codePoints = [];
  const skipped = [];
  for (const f of toIndex) {
    const raw = git.readAt(commit, f);
    if (raw == null || raw.includes('\u0000') || Buffer.byteLength(raw) > MAX_FILE_BYTES) { skipped.push(f); continue; }
    if (incremental) await codeStore.deleteByPath(repo, f);
    for (const [i, c] of chunkCode(f, scrub(raw)).entries()) {
      if (codePoints.length >= maxChunks) break;
      codePoints.push({ id: pointId(repo, f, i), text: c.text, payload: { repo, path: f, start: c.start, end: c.end, commit, lang: langOf(f), kind: 'code', text: c.text } });
    }
  }
  const codeVecs = await embedAll(llm, codePoints.map((p) => p.text));
  await codeStore.upsert(codePoints.map((p, i) => ({ id: p.id, vector: codeVecs[i], payload: p.payload })));

  // Semantic side: the repo's own docs/README and the declared page briefs.
  const docPoints = [];
  for (const f of docFiles.filter(wanted)) {
    const raw = git.readAt(commit, f);
    if (raw == null) continue;
    if (incremental) await docStore.deleteByPath(repo, f);
    for (const [i, c] of chunkMarkdown(raw).entries()) {
      docPoints.push({ id: pointId(repo, `src:${f}`, i), text: `${c.heading}\n${c.text}`, payload: { repo, path: f, chunk: i, heading: c.heading, text: c.text, commit, kind: 'source_doc' } });
    }
  }
  for (const pg of pages.filter((x) => x.brief)) {
    docPoints.push({ id: pointId(repo, `brief:${pg.path}`, 0), text: `${pg.path}\n${pg.brief}`, payload: { repo, path: `brief:${pg.path}`, chunk: 0, heading: `Brief: ${pg.path}`, text: pg.brief, commit, kind: 'brief' } });
  }
  const docVecs = await embedAll(llm, docPoints.map((p) => p.text));
  await docStore.upsert(docPoints.map((p, i) => ({ id: p.id, vector: docVecs[i], payload: p.payload })));

  const totalChunks = await codeStore.count(repo);
  await facts.setContextState(repo, { commit, files: codeFiles.length, chunks: totalChunks });

  return {
    mode: incremental ? 'incremental' : 'full',
    reason: incremental ? `index was at ${before.slice(0, 7)}` : (full ? 'forced' : !before ? 'no previous commit' : !state ? 'first run for this repo' : `index at ${state.commit.slice(0, 7)} but previous commit is ${before.slice(0, 7)}`),
    repoFiles: codeFiles.length, filesIndexed: toIndex.length - skipped.length, filesSkipped: skipped.length, removed,
    codeChunks: codePoints.length, docChunks: docPoints.length, ms: Date.now() - started,
  };
}

/**
 * Index the pages of the central documentation site itself (kind site_doc) under the key `site:<repo>`.
 * They are the semantic context for terminology, structure and what is already covered elsewhere.
 * Re-indexed only when the site's commit changes.
 */
async function syncSite({ siteRepo, commit, files, readFile, docStore, llm, facts }) {
  const key = `site:${siteRepo}`;
  const state = await facts.getContextState(key);
  if (state && state.commit === commit) return { skipped: true, pages: state.files, chunks: state.chunks, reason: `site already indexed at ${commit.slice(0, 7)}` };
  await docStore.deleteByRepo(key);
  const pts = [];
  for (const f of files) {
    const raw = readFile(f);
    if (raw == null) continue;
    for (const [i, c] of chunkMarkdown(raw).entries()) {
      pts.push({ id: pointId(key, f, i), text: `${c.heading}\n${c.text}`, payload: { repo: key, path: f, chunk: i, heading: c.heading, text: c.text, commit, kind: 'site_doc' } });
    }
  }
  const vecs = await embedAll(llm, pts.map((p) => p.text));
  await docStore.upsert(pts.map((p, i) => ({ id: p.id, vector: vecs[i], payload: p.payload })));
  await facts.setContextState(key, { commit, files: files.length, chunks: pts.length });
  return { skipped: false, pages: files.length, chunks: pts.length };
}

/**
 * Retrieve code chunks relevant to the given queries from across the whole repo, skipping files the
 * caller already has in full, within a character budget.
 */
async function retrieveCode({ llm, store, repo, queries, topK, exclude = [], budgetChars = 30000, minScore = 0 }) {
  const vecs = await llm.embed(queries.filter(Boolean));
  const seen = new Map();
  for (const v of vecs) {
    for (const h of await store.search(v, { limit: topK * 2, repo, kind: 'code' })) {
      if (exclude.includes(h.payload.path) || h.score < minScore) continue; // marginal matches are noise, not context
      const prev = seen.get(h.id);
      if (!prev || h.score > prev.score) seen.set(h.id, { id: h.id, score: h.score, path: h.payload.path, start: h.payload.start, end: h.payload.end, text: h.payload.text });
    }
  }
  const ranked = [...seen.values()].sort((a, b) => b.score - a.score).slice(0, topK);
  const out = [];
  let used = 0;
  for (const c of ranked) {
    if (used + c.text.length > budgetChars) break;
    used += c.text.length;
    out.push(c);
  }
  return out;
}

/** Semantic context: approved pages, source docs and briefs for this repo. */
async function retrieveSemantic({ llm, store, repo, queries, topK, siteRepo, siteTopK = 3, minScore = 0 }) {
  const vecs = await llm.embed(queries.filter(Boolean));
  const seen = new Map();
  for (const v of vecs) {
    const hits = await store.search(v, { limit: topK * 2, repo, kind: ['approved', 'source_doc', 'brief'] });
    // the rest of the documentation site: terminology, structure, what is already covered elsewhere
    if (siteRepo) hits.push(...await store.search(v, { limit: siteTopK, repo: `site:${siteRepo}`, kind: 'site_doc' }));
    for (const h of hits) {
      if (h.payload.kind !== 'brief' && h.score < minScore) continue; // the page brief is always kept; everything else must be relevant
      const prev = seen.get(h.id);
      if (!prev || h.score > prev.score) seen.set(h.id, { id: h.id, score: h.score, kind: h.payload.kind, path: h.payload.path, heading: h.payload.heading, text: h.payload.text });
    }
  }
  return [...seen.values()].sort((a, b) => b.score - a.score).slice(0, topK + (siteRepo ? siteTopK : 0));
}

module.exports = { syncContext, syncSite, retrieveCode, retrieveSemantic, DOC_FILE };
