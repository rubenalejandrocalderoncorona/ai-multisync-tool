#!/usr/bin/env node
'use strict';
/**
 * Index human-approved documentation into the vector store.
 *
 * Runs when a docs-sync PR is merged (see sync-docs-approved.yml). This is the ONLY path,
 * besides auto-trust publishing, that writes to the index — drafts and GAR text never do.
 *
 * Usage: node scripts/index_approved.js <file.md> [more.md ...]
 *        node scripts/index_approved.js --delete <removed-file.md> ...   (drop pages removed by the PR)
 *   Provenance (repo, source path, commit) comes from the file's front matter.
 */
const fs = require('fs');
const { indexApproved } = require('../pipeline/vectorstore');
const { recordDocRefs } = require('../pipeline/context');
const { buildDeps } = require('./lib');

function provenance(content) {
  const fm = (content.match(/^---\n([\s\S]*?)\n---/) || [])[1] || '';
  const source = (fm.match(/^source:\s*(\S+)/m) || [])[1] || '';
  const commit = (fm.match(/^commit:\s*(\S+)/m) || [])[1] || '';
  const docKey = (fm.match(/^doc_key:\s*(.+)$/m) || [])[1]?.trim();
  const m = source.match(/^https?:\/\/[^/]+\/([^/]+\/[^/]+)\/(?:blob\/[^/]+\/(.+)|tree\/[^/]+\/?)$/);
  if (!m) return null;
  const filePath = docKey || m[2];
  return filePath ? { repo: m[1], filePath, commit } : null;
}

async function main() {
  const args = process.argv.slice(2);
  const del = args[0] === '--delete';
  const files = del ? args.slice(1) : args;
  if (!files.length) { console.log('no files to index'); return; }
  const d = buildDeps();
  await d.facts.migrate();
  await d.vectors.ensureCollection();
  for (const f of files) {
    if (!fs.existsSync(f)) continue;
    const content = fs.readFileSync(f, 'utf-8');
    const p = provenance(content);
    if (!p) { console.warn(`skip ${f}: no provenance front matter (not an auto-synced page)`); continue; }
    if (del) {
      await d.vectors.deleteByPath(p.repo, p.filePath);
      console.log(`removed ${p.repo}/${p.filePath} from the index`);
      continue;
    }
    const n = await indexApproved({ store: d.vectors, llm: d.llm, content, ...p });
    await d.facts.approveClaims(p.repo, p.filePath, p.commit);
    if (d.facts.knownSymbolNames) await recordDocRefs(d.facts, p.repo, p.filePath, content, 'approved', await d.facts.knownSymbolNames());
    console.log(`indexed ${f}: ${n} chunk(s) @ ${p.commit.slice(0, 7)}`);
  }
  await d.facts.close();
}

main().catch((e) => { console.error('fatal:', e); process.exit(1); });
