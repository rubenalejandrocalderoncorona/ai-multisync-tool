#!/usr/bin/env node
'use strict';
/**
 * See what is in the vector database, and what a query would retrieve from it.
 *
 *   node scripts/context_search.js --status
 *   node scripts/context_search.js --repo owner/name --query "how are polls created" [--kind code|semantic|both] [--k 6]
 *   node scripts/context_search.js --repo owner/name --gar "The service lets participants vote on meeting times."
 *
 * Code context comes from the code collection; semantic context (approved pages, source docs, briefs, and the rest of
 * the documentation site) from the docs collection. --gar is the same retrieval the pipeline does with a hypothetical
 * documentation paragraph.
 */
const { retrieveCode, retrieveSemantic } = require('../pipeline/context');

async function searchContext({ llm, codeStore, docStore, repo, query, kind = 'both', k = 6, siteRepo }) {
  const out = {};
  if (kind === 'code' || kind === 'both') out.code = await retrieveCode({ llm, store: codeStore, repo, queries: [query], topK: k, budgetChars: 1e9 });
  if (kind === 'semantic' || kind === 'both') out.semantic = await retrieveSemantic({ llm, store: docStore, repo, queries: [query], topK: k, siteRepo });
  return out;
}

async function contextStatus({ codeStore, docStore, facts }) {
  const states = await facts.listContextState();
  const rows = [];
  for (const st of states) {
    const isSite = st.repo.startsWith('site:');
    rows.push({
      repo: st.repo, commit: String(st.commit).slice(0, 7), files: st.files,
      code: isSite ? '-' : await codeStore.count(st.repo, 'code'),
      source_doc: isSite ? '-' : await docStore.count(st.repo, 'source_doc'),
      brief: isSite ? '-' : await docStore.count(st.repo, 'brief'),
      approved: isSite ? '-' : await docStore.count(st.repo, 'approved'),
      site_doc: isSite ? await docStore.count(st.repo, 'site_doc') : '-',
    });
  }
  return rows;
}

const excerpt = (t, n = 110) => String(t).replace(/^FILE .*\n/, '').replace(/\s+/g, ' ').slice(0, n);

async function main() {
  const { buildDeps } = require('./lib');
  const arg = (n) => { const i = process.argv.indexOf(`--${n}`); return i > -1 ? process.argv[i + 1] : undefined; };
  const d = buildDeps();
  if (process.argv.includes('--status')) {
    console.table(await contextStatus({ codeStore: d.codeVectors, docStore: d.vectors, facts: d.facts }));
  } else {
    const repo = arg('repo'); const query = arg('query') || arg('gar');
    if (!repo || !query) throw new Error('usage: --status | --repo owner/name (--query "..." | --gar "paragraph") [--kind code|semantic|both] [--k 6]');
    const r = await searchContext({ llm: d.llm, codeStore: d.codeVectors, docStore: d.vectors, repo, query, kind: arg('kind') || 'both', k: Number(arg('k') || 6), siteRepo: process.env.SITE_REPO });
    for (const c of r.code || []) console.log(`CODE      ${c.score.toFixed(3)}  ${c.path}:${c.start}-${c.end}\n          ${excerpt(c.text)}`);
    for (const c of r.semantic || []) console.log(`SEMANTIC  ${c.score.toFixed(3)}  [${c.kind}] ${c.path}${c.heading ? ` > ${c.heading}` : ''}\n          ${excerpt(c.text)}`);
    if (!(r.code || []).length && !(r.semantic || []).length) console.log('no results: is this repo loaded? run --status');
  }
  await d.facts.close();
}

if (require.main === module) main().catch((e) => { console.error('fatal:', e.message); process.exit(1); });
module.exports = { searchContext, contextStatus };
