#!/usr/bin/env node
'use strict';
/** Verify Qdrant and the FactStore are reachable, create schema/collection, and exit non-zero on failure. */
const { buildDeps } = require('./lib');

async function main() {
  const d = buildDeps();
  const checks = [
    ['qdrant (semantic collection)', async () => { await d.vectors.ensureCollection(); return d.vectors.health(); }],
    ['qdrant (code collection)', async () => { await d.codeVectors.ensureCollection(); return d.codeVectors.health(); }],
    ['factstore', async () => { await d.facts.migrate(); return d.facts.health(); }],
  ];
  let ok = true;
  for (const [name, fn] of checks) {
    try { console.log(`${(await fn()) ? 'ok  ' : 'FAIL'} ${name}`); }
    catch (e) { ok = false; console.log(`FAIL ${name}: ${e.message}`); }
  }
  await d.facts.close();
  process.exit(ok ? 0 : 1);
}

main();
