#!/usr/bin/env node
'use strict';
/**
 * The FactStore: claims, decisions, the per-stage audit log, and the code -> docs coupling the model router reads.
 *
 *   node scripts/factstore.js --migrate                                   create or update the schema (idempotent)
 *   node scripts/factstore.js --status                                    row counts and the coupling per repo
 *   node scripts/factstore.js --symbol createPoll                         where a symbol is defined and which documents mention it
 *   node scripts/factstore.js --backfill --repo owner/name --dir <checkout> [--site-dir <docs repo> --site-repo owner/docs]
 *                                                                          fill symbols and doc references for a repo that is already loaded (no embeddings, no cost)
 */
const { repoPolicy, readJson, loadConfig } = require('../pipeline/config');
const { backfillCoupling } = require('../pipeline/context');
const { pagesScope } = require('../pipeline/codesource');
const { siteFiles: listSite, readSiteFile, siteCommit } = require('./sitedocs');

const arg = (n) => { const i = process.argv.indexOf(`--${n}`); return i > -1 ? process.argv[i + 1] : undefined; };
const has = (n) => process.argv.includes(`--${n}`);

async function main() {
  const { buildDeps } = require('./lib');
  const d = buildDeps();
  const f = d.facts;
  await f.migrate();
  if (has('migrate')) console.log('FactStore schema is up to date.');

  if (has('status')) {
    if (f.pool) {
      const counts = {};
      for (const t of ['claims', 'decisions', 'node_logs', 'context_state', 'symbols', 'doc_refs']) counts[t] = (await f.pool.query(`SELECT count(*)::int n FROM ${t}`)).rows[0].n;
      console.table([counts]);
    }
    console.log('Public symbols per repo:');
    console.table(await f.couplingStats());
  }

  if (arg('symbol')) {
    const info = await f.symbolInfo(arg('symbol'));
    console.log(`\nDefined in (${info.defined.length}):`);
    for (const x of info.defined.slice(0, 15)) console.log(`  ${x.repo}  ${x.path}  [${x.kind}]`);
    console.log(`Mentioned by (${info.referencedBy.length}):`);
    for (const x of info.referencedBy.slice(0, 15)) console.log(`  ${x.doc_repo}  ${x.doc_path}  [${x.kind}]`);
    if (!info.defined.length && !info.referencedBy.length) console.log('  (unknown symbol: is the repo loaded? try --backfill)');
  }

  if (has('backfill')) {
    const repo = arg('repo'); const dir = arg('dir');
    if (!repo || !dir) throw new Error('usage: --backfill --repo owner/name --dir <checkout> [--site-dir ... --site-repo ...]');
    const G = require('./gitutil');
    const cfg = loadConfig();
    const policy = repoPolicy(readJson(cfg.paths.reposConfig, { defaults: {}, repos: {} }), repo);
    let site = null;
    if (arg('site-dir') && arg('site-repo')) {
      const sd = arg('site-dir'); const read = readSiteFile(sd);
      site = new Map(listSite(sd).map((p) => [p, read(p)]).filter(([, t]) => t != null));
      site.repo = arg('site-repo');
    }
    const r = await backfillCoupling({ repo, commit: arg('ref') || G.head(dir), git: G.accessors(dir), facts: f, scope: pagesScope(policy.pages), exclude: policy.exclude || [], docs: policy.docs || [], siteFiles: site });
    console.log(`backfilled ${repo}: ${r.symbols} public symbols (${r.kinds.join(', ')}) from ${r.files} files; ${r.docRefs} doc references in ${r.docFiles} docs${site ? `, ${r.siteRefs} in the docs site` : ''}`);
  }
  await f.close();
}

main().catch((e) => { console.error('fatal:', e.message); process.exit(1); });
