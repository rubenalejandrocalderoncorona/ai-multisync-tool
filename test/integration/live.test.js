'use strict';
/**
 * Integration tests against REAL Qdrant and Postgres. Skipped unless both are configured:
 *   QDRANT_URL [QDRANT_API_KEY]  FACTSTORE_DATABASE_URL
 * Safe to point at a shared instance: every test uses unique names (random collections, `itest/<id>` repos)
 * and removes what it created. The LLM is a local fake, so no API key is needed.
 *
 *   QDRANT_URL=http://127.0.0.1:6333 FACTSTORE_DATABASE_URL=postgres://... npm run test:integration
 */
const test = require('node:test');
const assert = require('node:assert');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');
const { QdrantStore } = require('../../pipeline/vectorstore');
const { PgFactStore } = require('../../pipeline/factstore');
const { startFakeOpenAI } = require('../fake-openai');

const LIVE = !!(process.env.QDRANT_URL && process.env.FACTSTORE_DATABASE_URL);
const opts = { skip: LIVE ? false : 'set QDRANT_URL and FACTSTORE_DATABASE_URL to run' };
const id = () => crypto.randomBytes(4).toString('hex');
const qcfg = (collection) => ({ url: process.env.QDRANT_URL.replace(/\/$/, ''), apiKey: process.env.QDRANT_API_KEY || '', collection });
const headers = () => (process.env.QDRANT_API_KEY ? { 'api-key': process.env.QDRANT_API_KEY } : {});
const dropCollection = (name) => fetch(`${qcfg(name).url}/collections/${name}`, { method: 'DELETE', headers: headers() }).catch(() => {});
const vec = (i, dim = 8) => Array.from({ length: dim }, (_, k) => (k === i % dim ? 1 : 0.01));

test('Qdrant (real): collection, upsert, filtered search, re-key, delete, count', opts, async () => {
  const name = `itest_${id()}`;
  const q = new QdrantStore(qcfg(name), 8);
  try {
    assert.ok(await q.health());
    await q.ensureCollection();
    await q.ensureCollection(); // idempotent
    const uid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
    await q.upsert([
      { id: uid(1), vector: vec(0), payload: { repo: 'o/a', path: 'x.md', kind: 'approved', commit: 'c1', text: 'one' } },
      { id: uid(2), vector: vec(1), payload: { repo: 'o/a', path: 'y.go', kind: 'code', commit: 'c1', text: 'two' } },
      { id: uid(3), vector: vec(0), payload: { repo: 'o/b', path: 'x.md', kind: 'approved', commit: 'c1', text: 'three' } },
    ]);
    assert.strictEqual(await q.count(), 3);
    const hits = await q.search(vec(0), { limit: 5, repo: 'o/a' });
    assert.ok(hits.every((h) => h.payload.repo === 'o/a'), 'repo filter');
    assert.strictEqual(hits[0].payload.text, 'one');
    assert.deepStrictEqual((await q.search(vec(1), { repo: 'o/a', kind: 'code' })).map((h) => h.payload.path), ['y.go']);
    assert.deepStrictEqual((await q.search(vec(0), { repo: 'o/a', kind: ['approved', 'brief'] })).map((h) => h.payload.path), ['x.md']);
    assert.strictEqual((await q.search(vec(0), { repo: 'o/a', path: 'nope.md' })).length, 0);
    await q.touchCommit([uid(1)], 'c2');
    assert.strictEqual((await q.search(vec(0), { repo: 'o/a', kind: 'approved' }))[0].payload.commit, 'c2');
    await q.deleteByPath('o/a', 'x.md');
    assert.strictEqual(await q.count('o/a'), 1);
    await q.deleteByRepo('o/a', 'code');
    assert.strictEqual(await q.count('o/a'), 0);
    assert.strictEqual(await q.count('o/b'), 1, 'other repos untouched');
  } finally { await dropCollection(name); }
});

test('FactStore (real Postgres): schema is isolated, migrate is idempotent, claims/decisions/logs/context state round-trip', opts, async () => {
  const f = new PgFactStore(process.env.FACTSTORE_DATABASE_URL);
  const repo = `itest/${id()}`;
  try {
    await f.migrate();
    await f.migrate();
    assert.ok(await f.health());
    const schema = (await f.pool.query("SELECT table_schema FROM information_schema.tables WHERE table_name='claims'")).rows.map((r) => r.table_schema);
    assert.ok(schema.includes('multisync'), 'tables live in the multisync schema');
    assert.strictEqual((await f.pool.query('SELECT current_schema() AS s')).rows[0].s, 'multisync', 'every connection is pinned to the schema');

    await f.saveClaims({ repo, filePath: 'a.md', commit: 'c1', claims: [{ text: 'port is 8080 for alert_channels_enum', supported: true }, { text: 'bad', supported: false }] });
    assert.deepStrictEqual(await f.approvedClaims(repo, 'a.md'), [], 'nothing is trusted before approval');
    assert.strictEqual(await f.repoDocumentsSymbol(repo, 'alert_channels_enum'), false);
    assert.strictEqual((await f.pool.query('SELECT count(*)::int AS n FROM multisync.claims WHERE repo=$1', [repo])).rows[0].n, 2, 'rows land in multisync.claims');
    await f.approveClaims(repo, 'a.md', 'c1');
    assert.deepStrictEqual(await f.approvedClaims(repo, 'a.md'), ['port is 8080 for alert_channels_enum'], 'only supported claims are approved');
    assert.strictEqual(await f.repoDocumentsSymbol(repo, 'ALERT_CHANNELS_enum'), true, 'case-insensitive cross-repo lookup');

    await f.recordDecision({ runId: 'r1', repo, path: 'a.md', commit: 'c1', outcome: 'fallback', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded', reason: 'x', metrics: { a: 1 }, attempts: [{ n: 1 }] });
    assert.deepStrictEqual(await f.rootCauseBacklog(repo), [{ root_cause_tag: 'iteration_cap_exceeded', n: 1 }]);

    await f.recordNodeLog({ runId: 'r1', repo, path: 'a.md', commit: 'c1', node: 'judge', status: 'ok', ms: 12, note: { precision: 1 } });
    const log = (await f.pool.query('SELECT node, note FROM node_logs WHERE run_id=$1 AND repo=$2', ['r1', repo])).rows[0];
    assert.strictEqual(log.node, 'judge');
    assert.strictEqual(log.note.precision, 1, 'jsonb note round-trips');

    assert.strictEqual(await f.getContextState(repo), null);
    await f.setContextState(repo, { commit: 'c1', files: 3, chunks: 9 });
    await f.setContextState(repo, { commit: 'c2', files: 4, chunks: 12 });
    assert.deepStrictEqual(await f.getContextState(repo), { repo, commit: 'c2', files: 4, chunks: 12 });
  } finally {
    for (const t of ['claims', 'decisions', 'node_logs', 'context_state']) await f.pool.query(`DELETE FROM ${t} WHERE repo=$1`, [repo]);
    await f.close();
  }
});

const run = (args, o) => new Promise((resolve) => {
  const p = spawn('node', args, o); let out = '';
  p.stdout.on('data', (d) => (out += d)); p.stderr.on('data', (d) => (out += d));
  p.on('close', (code) => resolve({ code, out }));
});

test('END TO END (real Qdrant + Postgres, fake LLM): bootstrap whole context, then a code-mode run publishes a page, logs every stage', opts, async () => {
  const { server, hits, url } = await startFakeOpenAI();
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'msync-live-'));
  const src = path.join(work, 'source-repo');
  const sh = (...a) => execFileSync('git', ['-C', src, ...a], { encoding: 'utf-8' });
  const repo = `itest/${id()}`;
  const docsC = `itest_docs_${id()}`; const codeC = `itest_code_${id()}`;
  const f = new PgFactStore(process.env.FACTSTORE_DATABASE_URL);
  const runId = `itest-${id()}`;
  try {
    fs.mkdirSync(path.join(src, 'src'), { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', src]);
    sh('config', 'user.email', 't@t'); sh('config', 'user.name', 't');
    fs.writeFileSync(path.join(src, 'README.md'), '# Alerts\n\nAn alert service.\n');
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\n');
    fs.writeFileSync(path.join(src, 'src/b.go'), 'package main\nfunc Other() { Alert() }\n');
    sh('add', '-A'); sh('commit', '-qm', 'one');
    const c1 = sh('rev-parse', 'HEAD').trim();
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n');
    sh('add', '-A'); sh('commit', '-qm', 'two');
    const c2 = sh('rev-parse', 'HEAD').trim();

    fs.writeFileSync(path.join(work, 'repos.json'), JSON.stringify({ repos: { [repo]: { mode: 'code', trust: 'auto', serviceName: 'proj', pages: [{ path: 'api.md', kind: 'API documentation', brief: 'Explain the alert API.' }] } } }));
    const env = {
      ...process.env, AI_API_BASE_URL: url, INTERNAL_AI_API_KEY: 'x', AI_EMBED_DIM: '64',
      QDRANT_COLLECTION: docsC, QDRANT_CODE_COLLECTION: codeC, REPOS_CONFIG: path.join(work, 'repos.json'), FEATURE_REGISTRY: path.join(work, 'none.json'),
      DOCS_ROOT: 'site/docs', TEMPLATES_PATH: path.join(__dirname, '../../docs/templates'), INSTRUCTIONS_FILE: path.join(__dirname, '../../.github/instructions/DocumentationInstructions.instructions.md'),
      SOURCE_REPO: repo, SOURCE_DIR: src, CHANGED_FILES: '', TICKET_PROVIDER: 'none', RUN_ID: runId,
    };
    delete env.VECTOR_DRIVER; delete env.FACTSTORE_DRIVER;

    // 1. bootstrap: the WHOLE context is in the vector DB before any sync
    const boot = await run([path.join(__dirname, '../../scripts/bootstrap_context.js'), '--repo', repo, '--dir', src, '--ref', c1], { cwd: work, env });
    assert.strictEqual(boot.code, 0, boot.out);
    assert.match(boot.out, /bootstrapped .*: 2 files, \d+ code chunks, \d+ semantic chunks/);
    const code = new QdrantStore(qcfg(codeC), 64, undefined, codeC); const docs = new QdrantStore(qcfg(docsC), 64, undefined, docsC);
    const codeAtBoot = await code.count(repo);
    assert.ok(codeAtBoot >= 2, 'both source files are in the code collection');
    assert.ok(await docs.count(repo) >= 2, 'README and the page brief are in the semantic collection');
    assert.strictEqual((await f.getContextState(repo)).commit, c1);

    // 2. a real sync of commit c2
    const r = await run([path.join(__dirname, '../../scripts/run_pipeline.js')], { cwd: work, env: { ...env, SOURCE_SHA: c2, SOURCE_BEFORE: c1 } });
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /\[sync_context\] ok \d+ms \{"mode":"incremental"/);
    for (const n of ['prefilter', 'cross_repo', 'similarity', 'code_context', 'gar', 'semantic_context', 'write_draft', 'judge', 'publish']) assert.match(r.out, new RegExp(`\\[${n}\\]`));
    const res = JSON.parse(fs.readFileSync(path.join(work, 'pipeline-results.json'), 'utf-8')).results[0];
    assert.strictEqual(res.outcome, 'published');
    assert.ok(fs.existsSync(path.join(work, res.targetPath)));
    assert.strictEqual((await f.getContextState(repo)).commit, c2, 'index advanced to the new commit');
    assert.ok(hits.analyze === 1 && hits.plan === 1 && hits.judge >= 1);

    // 3. audit trail is in Postgres, in order, and the approved page is now in the semantic index
    const nodes = (await f.pool.query('SELECT node FROM node_logs WHERE run_id=$1 AND repo=$2 ORDER BY id', [runId, repo])).rows.map((x) => x.node);
    assert.deepStrictEqual(nodes, ['sync_context', 'prefilter', 'cross_repo', 'similarity', 'code_context', 'gar', 'semantic_context', 'write_draft', 'judge', 'publish']);
    assert.strictEqual((await f.pool.query('SELECT outcome FROM decisions WHERE run_id=$1 AND repo=$2', [runId, repo])).rows[0].outcome, 'published');
    const approved = await docs.search(Array(64).fill(0.1), { limit: 20, repo, kind: 'approved' });
    assert.ok(approved.length >= 1, 'auto-trust publish indexed the approved page');
    assert.ok((await f.approvedClaims(repo, 'api.md')).length >= 1, 'claims were approved into the FactStore');
  } finally {
    server.close();
    for (const c of [docsC, codeC]) await dropCollection(c);
    for (const t of ['claims', 'decisions', 'node_logs', 'context_state']) await f.pool.query(`DELETE FROM ${t} WHERE repo=$1`, [repo]).catch(() => {});
    await f.close();
    fs.rmSync(work, { recursive: true, force: true });
  }
});
