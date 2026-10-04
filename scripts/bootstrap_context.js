#!/usr/bin/env node
'use strict';
/**
 * Load the WHOLE context of a repository into the vector database, before any sync runs.
 * (A normal sync also self-heals the index, so this is for the first load, rebuilds and rehearsal.)
 *
 *   node scripts/bootstrap_context.js --repo owner/name --dir /path/to/checkout [--ref <sha>] [--full]
 *
 * Code context  -> code collection     (every in-scope source file, chunked)
 * Semantic ctx  -> docs collection     (the repo's README/docs as kind=source_doc, page briefs as kind=brief)
 */
const path = require('path');
const { loadConfig, repoPolicy } = require('../pipeline/config');
const { syncContext } = require('../pipeline/context');
const { pagesScope } = require('../pipeline/codesource');
const G = require('./gitutil');
const { buildDeps } = require('./lib');

const arg = (n) => { const i = process.argv.indexOf(`--${n}`); return i > -1 ? process.argv[i + 1] : undefined; };

async function main() {
  const repo = arg('repo');
  const dir = path.resolve(arg('dir') || '.');
  if (!repo) throw new Error('usage: bootstrap_context.js --repo owner/name --dir <checkout> [--ref sha] [--full]');
  const d = buildDeps();
  const commit = arg('ref') || G.head(dir);
  const policy = repoPolicy(d.reposConfig, repo);
  await d.facts.migrate();
  await d.vectors.ensureCollection();
  await d.codeVectors.ensureCollection();
  const stats = await syncContext({
    repo, commit, before: '', full: true, git: G.accessors(dir),
    codeStore: d.codeVectors, docStore: d.vectors, llm: d.llm, facts: d.facts, pages: policy.pages || [], scope: pagesScope(policy.pages), exclude: policy.exclude || [],
  });
  console.log(`bootstrapped ${repo}@${commit.slice(0, 7)}: ${stats.repoFiles} files, ${stats.codeChunks} code chunks, ${stats.docChunks} semantic chunks, ${stats.ms}ms`);
  console.log(`vector DB now holds ${await d.codeVectors.count(repo)} code chunks and ${await d.vectors.count(repo)} semantic chunks for ${repo}`);
  await d.facts.close();
}

main().catch((e) => { console.error('fatal:', e.message); process.exit(1); });
